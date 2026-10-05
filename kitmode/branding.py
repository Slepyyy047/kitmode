"""Operator-invoked Telegram profile setup; never runs during normal polling."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, TypedDict

from .transport import TelegramAPI

COMMANDS = ("start", "menu", "new", "today", "habits", "progress", "settings", "help", "cancel")


class ProfileCopy(TypedDict):
    name: str
    short_description: str
    description: str
    commands: tuple[str, ...]


PROFILE: dict[str, ProfileCopy] = {
    "uk": {
        "name": "KitMode 🐾 Звички без тиску",
        "short_description": "🐾 Маленькі кроки до корисних звичок. Відмітки, нагадування й прогрес у твоєму темпі.",
        "description": "Привіт, я KitMode 🐾\nДопоможу зробити корисні звички частиною твого дня — маленькими кроками, у своєму темпі.\n\n🌱 Додай звичку або обери готову.\n✅ Відмічай результат кількома натисканнями.\n📈 Дивись, як рухаєшся вперед.\n\nМожна почати з малого, взяти паузу й повернутися.\nНатисни «Почати» — я проведу тебе далі.",
        "commands": (
            "🐾 Почати знайомство",
            "🏠 Головне меню",
            "🌱 Додати звичку",
            "✅ План на сьогодні",
            "🐾 Мої звички",
            "📈 Мій прогрес",
            "⚙️ Налаштувати під себе",
            "❓ Як це працює",
            "↩️ Зупинити поточне налаштування",
        ),
    },
    "ru": {
        "name": "KitMode 🐾 Привычки без давления",
        "short_description": "🐾 Маленькие шаги к полезным привычкам. Отметки, напоминания и прогресс в твоём темпе.",
        "description": "Привет, я KitMode 🐾\nПомогу сделать полезные привычки частью твоего дня — маленькими шагами, в своём темпе.\n\n🌱 Добавь привычку или выбери готовую.\n✅ Отмечай результат несколькими нажатиями.\n📈 Смотри, как движешься вперёд.\n\nМожно начать с малого, взять паузу и вернуться.\nНажми «Начать» — я проведу тебя дальше.",
        "commands": (
            "🐾 Познакомиться",
            "🏠 Главное меню",
            "🌱 Добавить привычку",
            "✅ План на сегодня",
            "🐾 Мои привычки",
            "📈 Мой прогресс",
            "⚙️ Настроить под себя",
            "❓ Как это работает",
            "↩️ Остановить текущую настройку",
        ),
    },
    "en": {
        "name": "KitMode 🐾 Habits at your pace",
        "short_description": "🐾 Small steps toward useful habits. Easy check-ins, reminders and progress at your pace.",
        "description": "Hi, I’m KitMode 🐾\nI’ll help you make useful habits part of your day — with small steps, at your own pace.\n\n🌱 Add a habit or choose a ready-made one.\n✅ Check in with a few taps.\n📈 See how you’re moving forward.\n\nStart small, take a break and come back whenever you like.\nTap Start and I’ll guide you through it.",
        "commands": (
            "🐾 Get started",
            "🏠 Main menu",
            "🌱 Add a habit",
            "✅ Today’s plan",
            "🐾 My habits",
            "📈 My progress",
            "⚙️ Make it yours",
            "❓ How it works",
            "↩️ Stop the current setup",
        ),
    },
}

TEXT_FIELDS: tuple[tuple[str, Literal["name", "description", "short_description"]], ...] = (
    ("MyName", "name"),
    ("MyDescription", "description"),
    ("MyShortDescription", "short_description"),
)


def command_list(lang: str) -> list[dict[str, str]]:
    return [
        {"command": command, "description": description}
        for command, description in zip(COMMANDS, PROFILE[lang]["commands"], strict=True)
    ]


def apply_profile(api: TelegramAPI, avatar: Path | None = None) -> dict[str, Any]:
    """Publish public profile copy/commands and verify Telegram's saved values."""
    for language in ("", "uk", "ru", "en"):
        lang = language or "uk"
        copy = PROFILE[lang]
        for suffix, field in TEXT_FIELDS:
            if api._request("set" + suffix, {field: copy[field], "language_code": language}) is not True:
                raise RuntimeError("Telegram did not confirm the profile update")
        for scope in ("default", "all_private_chats"):
            if (
                api._request(
                    "setMyCommands",
                    {"commands": command_list(lang), "scope": {"type": scope}, "language_code": language},
                )
                is not True
            ):
                raise RuntimeError("Telegram did not confirm the command menu")
    if api._request("setChatMenuButton", {"menu_button": {"type": "commands"}}) is not True:
        raise RuntimeError("Telegram did not confirm the menu button")
    if avatar:
        photo = json.dumps({"type": "static", "photo": "attach://avatar"})
        if (
            api._request("setMyProfilePhoto", {"photo": photo}, ("avatar", avatar.name, avatar.read_bytes()))
            is not True
        ):
            raise RuntimeError("Telegram did not confirm the avatar")
    for language in ("", "uk", "ru", "en"):
        lang = language or "uk"
        for suffix, field in TEXT_FIELDS:
            saved = api._request("get" + suffix, {"language_code": language})
            if saved.get(field) != PROFILE[lang][field]:
                raise RuntimeError("Telegram profile verification failed")
        for scope in ("default", "all_private_chats"):
            saved_commands = api._request("getMyCommands", {"scope": {"type": scope}, "language_code": language})
            if saved_commands != command_list(lang):
                raise RuntimeError("Telegram command verification failed")
    if api._request("getChatMenuButton", {}).get("type") != "commands":
        raise RuntimeError("Telegram menu-button verification failed")
    return {
        "languages": ["default", "uk", "ru", "en"],
        "commands": list(COMMANDS),
        "menu": "commands",
        "avatar_updated": bool(avatar),
    }
