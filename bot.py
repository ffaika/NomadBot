
import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram import types as tg_types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

from google import genai
from google.genai import types as genai_types

from dotenv import load_dotenv
load_dotenv()




# ===================== НАСТРОЙКИ =====================
TELEGRAM_TOKEN = "8304952125:AAHpWN8_W9SkGRye_Cs0USjYLthVfoVGBXo"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME     = "gemini-3-flash-preview"

TESTS_FILE = Path("tests.json")


# ===================== ХРАНИЛИЩЕ =====================
def load_tests() -> list[dict]:
    """
    Структура tests.json:
    [
      {
        "id": "t1",
        "name": "Тест 1",
        "topic": "Казахское ханство",
        "created_at": "15.01.2024 10:30",
        "author_id": 123456789,
        "author_name": "Иван",
        "questions": [
          {"text": "...", "options": ["А","Б","В","Г"], "correct": 0, "image_file_id": null}
        ]
      }
    ]
    """
    if TESTS_FILE.exists():
        with open(TESTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_tests(tests: list[dict]):
    with open(TESTS_FILE, "w", encoding="utf-8") as f:
        json.dump(tests, f, ensure_ascii=False, indent=2)


def get_test_by_id(test_id: str) -> dict | None:
    return next((t for t in load_tests() if t["id"] == test_id), None)


def next_test_id() -> str:
    tests = load_tests()
    nums  = [int(t["id"][1:]) for t in tests if t["id"][1:].isdigit()]
    return f"t{max(nums) + 1}" if nums else "t1"


# ===================== ИНИЦИАЛИЗАЦИЯ =====================
bot    = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp     = Dispatcher(storage=MemoryStorage())
client = genai.Client(api_key=GEMINI_API_KEY)

# Активные прохождения: user_id -> {test_id, test_name, idx, score, questions}
user_session: dict[int, dict] = {}


# ===================== FSM =====================
class CreateTest(StatesGroup):
    waiting_name     = State()
    waiting_question = State()

class GenTest(StatesGroup):
    waiting_name  = State()
    waiting_topic = State()
    waiting_count = State()


# ===================== ГЛАВНОЕ МЕНЮ =====================
def main_menu() -> tg_types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📚 Список тестов",            callback_data="show_tests")
    kb.button(text="✏️ Создать тест вручную",     callback_data="create_test")
    kb.button(text="🤖 Создать тест через AI",    callback_data="gen_test")
    kb.button(text="🗑 Удалить свой тест",         callback_data="delete_my_test")
    kb.adjust(1)
    return kb.as_markup()


@dp.message(Command("start"))
async def cmd_start(message: tg_types.Message):
    await message.answer(
        "<b>Nomad — бот по истории Казахстана 🇰🇿</b>\n\n"
        "Готовься к ЕНТ: проходи тесты и получай объяснения от ИИ.\n"
        "Любой может создавать и делиться тестами!\n\n"
        "Выбери действие:",
        reply_markup=main_menu(),
    )


@dp.message(Command("menu"))
async def cmd_menu(message: tg_types.Message):
    await message.answer("Главное меню:", reply_markup=main_menu())


# ===================== СПИСОК ТЕСТОВ =====================
def tests_list_keyboard(user_id: int) -> tg_types.InlineKeyboardMarkup:
    tests = load_tests()
    kb    = InlineKeyboardBuilder()
    if not tests:
        kb.button(text="Тестов пока нет — создай первый!", callback_data="noop")
    else:
        for t in tests:
            author = f" [{t['author_name']}]" if t.get("author_name") else ""
            label  = f"📋 {t['name']}"
            if t.get("topic"):
                label += f" · {t['topic']}"
            label += f"  ({len(t['questions'])} вопр.){author}"
            kb.button(text=label, callback_data=f"pick_test_{t['id']}")
    kb.button(text="◀️ Меню", callback_data="to_menu")
    kb.adjust(1)
    return kb.as_markup()


@dp.callback_query(lambda c: c.data == "show_tests")
async def show_tests(callback: tg_types.CallbackQuery):
    tests = load_tests()
    text  = (
        f"<b>📚 Тесты ({len(tests)})</b>\n\nВыбери тест для прохождения:"
        if tests else
        "<b>📚 Тестов пока нет</b>\n\nБудь первым — создай тест!"
    )
    await callback.message.edit_text(text, reply_markup=tests_list_keyboard(callback.from_user.id))
    await callback.answer()


@dp.message(Command("tests"))
async def cmd_tests(message: tg_types.Message):
    tests = load_tests()
    if not tests:
        text = "Тестов пока нет. Создай первый через /menu!"
    else:
        lines = ["<b>📚 Все тесты:</b>\n"]
        for i, t in enumerate(tests, 1):
            lines.append(
                f"{i}. <b>{t['name']}</b>"
                + (f" — {t['topic']}" if t.get("topic") else "")
                + f"\n   📝 {len(t['questions'])} вопр."
                + (f" · 👤 {t['author_name']}" if t.get("author_name") else "")
                + f" · 🗓 {t.get('created_at', '—')}"
            )
        text = "\n".join(lines)
    await message.answer(text, reply_markup=tests_list_keyboard(message.from_user.id))


# ===================== ВЫБОР И ЗАПУСК ТЕСТА =====================
@dp.callback_query(lambda c: c.data.startswith("pick_test_"))
async def pick_test(callback: tg_types.CallbackQuery):
    test_id = callback.data[len("pick_test_"):]
    test    = get_test_by_id(test_id)
    if not test:
        await callback.answer("Тест не найден.", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Начать тест", callback_data=f"start_test_{test_id}")
    kb.button(text="◀️ К списку",    callback_data="show_tests")
    kb.adjust(1)

    await callback.message.edit_text(
        f"<b>{test['name']}</b>\n"
        + (f"📌 Тема: {test['topic']}\n" if test.get("topic") else "")
        + f"📝 Вопросов: {len(test['questions'])}\n"
        + f"🗓 Создан: {test.get('created_at', '—')}\n"
        + (f"👤 Автор: {test['author_name']}" if test.get("author_name") else ""),
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("start_test_"))
async def start_test(callback: tg_types.CallbackQuery):
    test_id = callback.data[len("start_test_"):]
    test    = get_test_by_id(test_id)
    if not test or not test["questions"]:
        await callback.answer("Тест пустой или не найден.", show_alert=True)
        return

    user_id = callback.from_user.id
    user_session[user_id] = {
        "test_id":   test_id,
        "test_name": test["name"],
        "idx":       0,
        "score":     0,
        "questions": test["questions"],
    }
    await callback.answer()
    await callback.message.answer(
        f"🚀 Начинаем: <b>{test['name']}</b>\nВопросов: {len(test['questions'])}. Удачи! 💪"
    )
    await send_question(callback.message.chat.id, user_id)


# ===================== ПРОХОЖДЕНИЕ ТЕСТА =====================
async def send_question(chat_id: int, user_id: int):
    session   = user_session.get(user_id)
    if not session:
        return
    questions = session["questions"]
    i         = session["idx"]

    if i >= len(questions):
        score = session["score"]
        total = len(questions)
        pct   = round(score / total * 100)
        emoji = "🏆" if pct == 100 else ("🎉" if pct >= 80 else ("👍" if pct >= 60 else "📚"))
        await bot.send_message(
            chat_id,
            f"{emoji} <b>Тест завершён!</b>\n\n"
            f"📋 {session['test_name']}\n"
            f"Результат: <b>{score} из {total}</b> ({pct}%)\n\n"
            + ("Отлично! Ты готов к ЕНТ! 🔥" if pct >= 80 else "Повтори материал и попробуй снова! 📖")
        )
        user_session.pop(user_id, None)
        kb = InlineKeyboardBuilder()
        kb.button(text="📚 Все тесты", callback_data="show_tests_new")
        kb.button(text="🏠 Меню",       callback_data="to_menu_new")
        kb.adjust(2)
        await bot.send_message(chat_id, "Что дальше?", reply_markup=kb.as_markup())
        return

    q = questions[i]
    if q.get("image_file_id"):
        await bot.send_photo(chat_id=chat_id, photo=q["image_file_id"],
                             caption=f"[{i+1}/{len(questions)}] {q['text']}")
    await bot.send_poll(
        chat_id=chat_id,
        question=f"[{i+1}/{len(questions)}] {q['text']}",
        options=q["options"], type="quiz",
        correct_option_id=q["correct"],
        is_anonymous=False, open_period=45,
    )


@dp.poll_answer()
async def handle_poll_answer(poll_answer: tg_types.PollAnswer):
    user_id = poll_answer.user.id
    session = user_session.get(user_id)
    if not session:
        return
    questions = session["questions"]
    idx       = session["idx"]
    if idx >= len(questions):
        return

    q       = questions[idx]
    correct = (poll_answer.option_ids[0] == q["correct"])
    if correct:
        session["score"] += 1

    # Объяснение от Gemini
    prompt = (
        "Ты помогаешь школьникам готовиться к ЕНТ по истории Казахстана. "
        "Ответь очень кратко (1–2 предложения).\n\n"
        f"Вопрос: {q['text']}\n"
        f"Варианты: {', '.join(q['options'])}\n"
        f"Правильный ответ: {q['options'][q['correct']]}\n"
        "Кратко объясни, почему это правильный вариант."
    )
    try:
        resp        = client.models.generate_content(model=MODEL_NAME,
                          contents=genai_types.Part.from_text(text=prompt))
        explanation = resp.text
    except Exception as e:
        explanation = f"(Объяснение недоступно: {e})"

    result = "✅ <b>Правильно!</b>\n\n" if correct else \
             f"❌ <b>Неправильно.</b> Правильный ответ: <b>{q['options'][q['correct']]}</b>\n\n"
    await bot.send_message(user_id, result + explanation)
    session["idx"] += 1
    await send_question(user_id, user_id)


# ===================== СОЗДАНИЕ ТЕСТА ВРУЧНУЮ =====================
@dp.callback_query(lambda c: c.data == "create_test")
@dp.message(Command("add_test"))
async def start_create_test(update, state: FSMContext):
    if isinstance(update, tg_types.CallbackQuery):
        msg = update.message
        await update.answer()
        await state.update_data(author_id=update.from_user.id,
                                author_name=update.from_user.full_name)
    else:
        msg = update
        await state.update_data(author_id=update.from_user.id,
                                author_name=update.from_user.full_name)

    await msg.answer(
        "✏️ <b>Создание теста</b>\n\n"
        "Введи <b>название теста</b>:\n"
        "(например: «Казахское ханство», «Пробник по ВОВ»)\n\n"
        "Отмена — /cancel"
    )
    await state.set_state(CreateTest.waiting_name)


@dp.message(CreateTest.waiting_name)
async def create_test_set_name(message: tg_types.Message, state: FSMContext):
    await state.update_data(test_name=message.text.strip(), questions=[])
    await message.answer(
        f"✅ Название: <b>{message.text.strip()}</b>\n\n"
        "Теперь добавляй вопросы по одному.\n\n"
        "📝 <b>Формат</b> (одним сообщением):\n"
        "<code>Текст вопроса\n"
        "Вариант А; Вариант Б; Вариант В; Вариант Г\n"
        "Номер правильного (1–4)</code>\n\n"
        "Когда закончишь — /done_test\n"
        "Отмена — /cancel"
    )
    await state.set_state(CreateTest.waiting_question)


@dp.message(Command("done_test"), CreateTest.waiting_question)
async def create_test_finish(message: tg_types.Message, state: FSMContext):
    data      = await state.get_data()
    questions = data.get("questions", [])
    if not questions:
        await message.answer("Нет ни одного вопроса. Добавь хотя бы один или /cancel")
        return

    test = {
        "id":          next_test_id(),
        "name":        data["test_name"],
        "topic":       "",
        "created_at":  datetime.now().strftime("%d.%m.%Y %H:%M"),
        "author_id":   data["author_id"],
        "author_name": data["author_name"],
        "questions":   questions,
    }
    tests = load_tests()
    tests.append(test)
    save_tests(tests)
    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Пройти тест", callback_data=f"start_test_{test['id']}")
    kb.button(text="🏠 Меню",         callback_data="to_menu_new")
    kb.adjust(2)
    await message.answer(
        f"🎉 Тест <b>{test['name']}</b> опубликован!\n"
        f"📝 Вопросов: {len(questions)}\n\n"
        "Теперь его может пройти любой пользователь!",
        reply_markup=kb.as_markup(),
    )


@dp.message(CreateTest.waiting_question)
async def create_test_add_question(message: tg_types.Message, state: FSMContext):
    if not message.text:
        return
    lines = message.text.strip().split("\n")
    if len(lines) < 3:
        await message.answer(
            "Неверный формат. Нужно 3 строки:\n"
            "1 — текст вопроса\n2 — варианты через ;\n3 — номер правильного\n\nПопробуй ещё раз."
        )
        return
    try:
        q_text  = lines[0].strip()
        options = [o.strip() for o in lines[1].split(";")]
        correct = int(lines[2].strip()) - 1
        if len(options) < 2 or not (0 <= correct < len(options)):
            raise ValueError
    except Exception:
        await message.answer("Ошибка формата. Проверь номер правильного ответа.")
        return

    image_file_id = message.photo[-1].file_id if message.photo else None
    data          = await state.get_data()
    questions     = data.get("questions", [])
    questions.append({"text": q_text, "options": options,
                      "correct": correct, "image_file_id": image_file_id})
    await state.update_data(questions=questions)
    await message.answer(
        f"✅ Вопрос {len(questions)} добавлен.\n"
        "Отправь следующий или /done_test для завершения."
    )


# ===================== СОЗДАНИЕ ТЕСТА ЧЕРЕЗ AI =====================
@dp.callback_query(lambda c: c.data == "gen_test")
@dp.message(Command("gen_test"))
async def start_gen_test(update, state: FSMContext):
    if isinstance(update, tg_types.CallbackQuery):
        msg = update.message
        await update.answer()
        await state.update_data(author_id=update.from_user.id,
                                author_name=update.from_user.full_name)
    else:
        msg = update
        await state.update_data(author_id=update.from_user.id,
                                author_name=update.from_user.full_name)

    await msg.answer(
        "🤖 <b>Генерация теста через AI</b>\n\n"
        "Введи <b>название теста</b>:\n"
        "(например: «Пробник ЕНТ #1», «Тест по ВОВ»)\n\n"
        "Отмена — /cancel"
    )
    await state.set_state(GenTest.waiting_name)


@dp.message(GenTest.waiting_name)
async def gen_test_set_name(message: tg_types.Message, state: FSMContext):
    await state.update_data(test_name=message.text.strip())
    await message.answer(
        "По какой <b>теме</b> создать вопросы?\n\n"
        "Примеры:\n"
        "• Казахское ханство XV–XVIII вв.\n"
        "• Казахстан в годы ВОВ\n"
        "• Независимость Казахстана\n"
        "• Культура и наука Казахстана"
    )
    await state.set_state(GenTest.waiting_topic)


@dp.message(GenTest.waiting_topic)
async def gen_test_set_topic(message: tg_types.Message, state: FSMContext):
    await state.update_data(topic=message.text.strip())
    kb = InlineKeyboardBuilder()
    for n in [5, 10, 15, 20]:
        kb.button(text=str(n), callback_data=f"gen_count_{n}")
    kb.adjust(4)
    await message.answer(
        f"Тема: <b>{message.text.strip()}</b>\n\nСколько вопросов сгенерировать?",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(GenTest.waiting_count)


@dp.callback_query(lambda c: c.data.startswith("gen_count_"), GenTest.waiting_count)
async def gen_test_run(callback: tg_types.CallbackQuery, state: FSMContext):
    count     = int(callback.data.split("_")[-1])
    data      = await state.get_data()
    test_name = data["test_name"]
    topic     = data["topic"]
    user_id   = callback.from_user.id
    await state.clear()
    await callback.answer()

    await callback.message.answer(
        f"⏳ Генерирую <b>{count} вопросов</b> по теме «{topic}»...\nПодожди несколько секунд."
    )

    prompt = f"""Ты эксперт по истории Казахстана, составляешь задания для ЕНТ.

Сгенерируй {count} вопросов по теме: «{topic}»

Требования:
- Все вопросы на русском языке
- Уровень: средний и высокий (уровень ЕНТ)
- 4 варианта ответа на каждый вопрос
- Ровно один правильный вариант
- Проверяй знание конкретных фактов, дат, имён, событий

Верни ТОЛЬКО JSON без пояснений и markdown:
[
  {{"text": "Вопрос?", "options": ["Вариант А", "Вариант Б", "Вариант В", "Вариант Г"], "correct": 0}}
]
"correct" — индекс правильного ответа (0–3).
"""

    try:
        resp    = client.models.generate_content(
            model=MODEL_NAME, contents=genai_types.Part.from_text(text=prompt)
        )
        cleaned = re.sub(r"```(?:json)?", "", resp.text).strip().rstrip("`").strip()
        validated = [
            {"text": q["text"], "options": [str(o) for o in q["options"]],
             "correct": q["correct"], "image_file_id": None}
            for q in json.loads(cleaned)
            if isinstance(q.get("text"), str)
            and isinstance(q.get("options"), list) and len(q["options"]) == 4
            and isinstance(q.get("correct"), int) and 0 <= q["correct"] <= 3
        ]
        if not validated:
            raise ValueError("Не удалось разобрать ответ ИИ")
    except Exception as e:
        await bot.send_message(user_id, f"❌ Ошибка генерации: {e}\n\nПопробуй /gen_test ещё раз.")
        return

    test = {
        "id":          next_test_id(),
        "name":        test_name,
        "topic":       topic,
        "created_at":  datetime.now().strftime("%d.%m.%Y %H:%M"),
        "author_id":   user_id,
        "author_name": data["author_name"],
        "questions":   validated,
    }
    tests = load_tests()
    tests.append(test)
    save_tests(tests)

    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Пройти тест", callback_data=f"start_test_{test['id']}")
    kb.button(text="📚 Все тесты",    callback_data="show_tests_new")
    kb.button(text="🏠 Меню",         callback_data="to_menu_new")
    kb.adjust(2)
    await bot.send_message(
        user_id,
        f"✅ Тест <b>{test_name}</b> создан и опубликован!\n\n"
        f"📌 Тема: {topic}\n"
        f"📝 Вопросов: {len(validated)}\n\n"
        f"Пример: <i>{validated[0]['text']}</i>",
        reply_markup=kb.as_markup(),
    )


# ===================== УДАЛЕНИЕ СВОЕГО ТЕСТА =====================
@dp.callback_query(lambda c: c.data == "delete_my_test")
@dp.message(Command("delete_test"))
async def show_my_tests_for_delete(update, state: FSMContext):
    if isinstance(update, tg_types.CallbackQuery):
        user_id = update.from_user.id
        send    = update.message.edit_text
        await update.answer()
    else:
        user_id = update.from_user.id
        send    = update.answer

    tests    = load_tests()
    my_tests = [t for t in tests if t.get("author_id") == user_id]

    if not my_tests:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Меню", callback_data="to_menu")
        kb.adjust(1)
        await send("У тебя пока нет созданных тестов.", reply_markup=kb.as_markup())
        return

    kb = InlineKeyboardBuilder()
    for t in my_tests:
        kb.button(
            text=f"🗑 {t['name']} ({len(t['questions'])} вопр.)",
            callback_data=f"del_test_{t['id']}"
        )
    kb.button(text="◀️ Меню", callback_data="to_menu")
    kb.adjust(1)
    await send("Выбери тест для удаления:", reply_markup=kb.as_markup())


@dp.callback_query(lambda c: c.data.startswith("del_test_"))
async def del_test_confirm(callback: tg_types.CallbackQuery):
    test_id = callback.data[len("del_test_"):]
    test    = get_test_by_id(test_id)

    if not test:
        await callback.answer("Тест не найден.", show_alert=True)
        return
    if test.get("author_id") != callback.from_user.id:
        await callback.answer("Можно удалять только свои тесты.", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"confirm_del_{test_id}")
    kb.button(text="❌ Отмена",       callback_data="to_menu")
    kb.adjust(2)
    await callback.message.edit_text(
        f"Удалить тест <b>{test['name']}</b>?\n"
        f"({len(test['questions'])} вопросов)",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("confirm_del_"))
async def del_test_execute(callback: tg_types.CallbackQuery):
    test_id = callback.data[len("confirm_del_"):]
    tests   = load_tests()
    test    = next((t for t in tests if t["id"] == test_id), None)

    if not test:
        await callback.answer("Уже удалён.", show_alert=True)
        return
    if test.get("author_id") != callback.from_user.id:
        await callback.answer("Можно удалять только свои тесты.", show_alert=True)
        return

    save_tests([t for t in tests if t["id"] != test_id])
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Меню", callback_data="to_menu")
    kb.adjust(1)
    await callback.message.edit_text(
        f"🗑 Тест <b>{test['name']}</b> удалён.",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


# ===================== НАВИГАЦИЯ =====================
@dp.callback_query(lambda c: c.data in ("to_menu", "to_menu_new"))
async def to_menu(callback: tg_types.CallbackQuery):
    text = "Главное меню:"
    if callback.data == "to_menu":
        await callback.message.edit_text(text, reply_markup=main_menu())
    else:
        await callback.message.answer(text, reply_markup=main_menu())
    await callback.answer()


@dp.callback_query(lambda c: c.data in ("show_tests_new",))
async def show_tests_new(callback: tg_types.CallbackQuery):
    tests = load_tests()
    text  = f"<b>📚 Тесты ({len(tests)})</b>\n\nВыбери тест:" if tests else "Тестов пока нет."
    await callback.message.answer(text, reply_markup=tests_list_keyboard(callback.from_user.id))
    await callback.answer()


@dp.callback_query(lambda c: c.data == "noop")
async def noop(callback: tg_types.CallbackQuery):
    await callback.answer()


# ===================== ОТМЕНА =====================
@dp.message(Command("cancel"))
async def cancel_fsm(message: tg_types.Message, state: FSMContext):
    if await state.get_state():
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_menu())
    else:
        await message.answer("Нечего отменять.", reply_markup=main_menu())


# ===================== ЗАПУСК =====================
async def main():
    print("Nomad bot is running...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())




#🖕