import os
import io
import json
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import edge_tts
from pydub import AudioSegment


# ============================================================
# CONFIGURATION
# ============================================================

# Your bot token will be read from environment variables.
# Example on Railway:
# BOT_TOKEN=123456789:ABCDEF....
BOT_TOKEN = os.getenv("BOT_TOKEN")

# File used to save user preferences persistently.
# Since the project must be single-file, we use a local JSON file.
PREFERENCES_FILE = "user_prefs.json"

# Temporary folder for audio files
TEMP_DIR = Path("temp_audio")
TEMP_DIR.mkdir(exist_ok=True)

# Max text length protection to avoid very large processing requests.
MAX_TEXT_LENGTH = 3500

# Logging setup so you can debug errors in Railway logs.
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ============================================================
# LANGUAGE + VOICE CONFIG
# ============================================================
# We define supported languages and map male/female voices.
# You can expand this list later if you want.
#
# Voice names are Microsoft Edge TTS voices supported by edge-tts.
# If a voice becomes unavailable in the future, replace with a valid one.

LANGUAGE_VOICE_MAP = {
    "english": {
        "label": "English",
        "male": "en-US-GuyNeural",
        "female": "en-US-JennyNeural",
    },
    "bengali": {
        "label": "Bengali",
        "male": "bn-BD-PradeepNeural",
        "female": "bn-BD-NabanitaNeural",
    },
    "hindi": {
        "label": "Hindi",
        "male": "hi-IN-MadhurNeural",
        "female": "hi-IN-SwaraNeural",
    },
    "japanese": {
        "label": "Japanese",
        "male": "ja-JP-KeitaNeural",
        "female": "ja-JP-NanamiNeural",
    },
    "arabic": {
        "label": "Arabic",
        "male": "ar-SA-HamedNeural",
        "female": "ar-SA-ZariyahNeural",
    },
    "spanish": {
        "label": "Spanish",
        "male": "es-ES-AlvaroNeural",
        "female": "es-ES-ElviraNeural",
    },
}

DEFAULT_LANGUAGE = "english"
DEFAULT_GENDER = "female"


# ============================================================
# PREFERENCE STORAGE HELPERS
# ============================================================

def load_preferences() -> Dict[str, Dict[str, str]]:
    """
    Load saved user preferences from JSON file.
    Structure example:
    {
        "123456789": {
            "language": "english",
            "gender": "female"
        }
    }
    """
    if not os.path.exists(PREFERENCES_FILE):
        return {}

    try:
        with open(PREFERENCES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load preferences: {e}")
        return {}


def save_preferences(data: Dict[str, Dict[str, str]]) -> None:
    """
    Save user preferences to JSON file.
    """
    try:
        with open(PREFERENCES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save preferences: {e}")


def get_user_preferences(user_id: int) -> Dict[str, str]:
    """
    Get preferences for a user. If missing, return defaults.
    """
    prefs = load_preferences()
    user_key = str(user_id)

    if user_key not in prefs:
        prefs[user_key] = {
            "language": DEFAULT_LANGUAGE,
            "gender": DEFAULT_GENDER,
        }
        save_preferences(prefs)

    return prefs[user_key]


def update_user_preferences(user_id: int, language: Optional[str] = None, gender: Optional[str] = None) -> Dict[str, str]:
    """
    Update a user's language and/or gender preference.
    """
    prefs = load_preferences()
    user_key = str(user_id)

    if user_key not in prefs:
        prefs[user_key] = {
            "language": DEFAULT_LANGUAGE,
            "gender": DEFAULT_GENDER,
        }

    if language is not None:
        prefs[user_key]["language"] = language

    if gender is not None:
        prefs[user_key]["gender"] = gender

    save_preferences(prefs)
    return prefs[user_key]


# ============================================================
# KEYBOARD BUILDERS
# ============================================================

def build_main_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """
    Main settings keyboard with current selected values visible.
    """
    prefs = get_user_preferences(user_id)
    lang_key = prefs.get("language", DEFAULT_LANGUAGE)
    gender = prefs.get("gender", DEFAULT_GENDER)

    lang_label = LANGUAGE_VOICE_MAP.get(lang_key, LANGUAGE_VOICE_MAP[DEFAULT_LANGUAGE])["label"]
    gender_label = gender.capitalize()

    keyboard = [
        [
            InlineKeyboardButton(
                text=f"🌐 Language: {lang_label}",
                callback_data="open_language_menu"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"🧑 Gender: {gender_label}",
                callback_data="open_gender_menu"
            )
        ],
        [
            InlineKeyboardButton(
                text="✅ Done",
                callback_data="settings_done"
            )
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def build_language_keyboard() -> InlineKeyboardMarkup:
    """
    Inline keyboard for language selection.
    """
    keyboard = []
    row = []

    for i, (lang_key, info) in enumerate(LANGUAGE_VOICE_MAP.items(), start=1):
        row.append(
            InlineKeyboardButton(
                text=info["label"],
                callback_data=f"set_language:{lang_key}"
            )
        )
        # Put 2 buttons per row for a neat layout
        if i % 2 == 0:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_settings")])

    return InlineKeyboardMarkup(keyboard)


def build_gender_keyboard() -> InlineKeyboardMarkup:
    """
    Inline keyboard for gender selection.
    """
    keyboard = [
        [
            InlineKeyboardButton(text="👨 Male", callback_data="set_gender:male"),
            InlineKeyboardButton(text="👩 Female", callback_data="set_gender:female"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_settings")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


# ============================================================
# VOICE SELECTION
# ============================================================

def get_voice_for_user(user_id: int) -> Tuple[str, str, str]:
    """
    Return (language_key, gender, voice_name) for the user.
    """
    prefs = get_user_preferences(user_id)
    language = prefs.get("language", DEFAULT_LANGUAGE)
    gender = prefs.get("gender", DEFAULT_GENDER)

    if language not in LANGUAGE_VOICE_MAP:
        language = DEFAULT_LANGUAGE

    if gender not in ("male", "female"):
        gender = DEFAULT_GENDER

    voice_name = LANGUAGE_VOICE_MAP[language][gender]
    return language, gender, voice_name


# ============================================================
# TTS GENERATION
# ============================================================

async def generate_tts_ogg(text: str, voice_name: str) -> bytes:
    """
    Generate TTS audio using edge-tts, then convert it to OGG/Opus
    suitable for Telegram voice notes.

    Returns:
        bytes of the final OGG file.
    """
    unique_id = str(uuid.uuid4())
    mp3_path = TEMP_DIR / f"{unique_id}.mp3"
    ogg_path = TEMP_DIR / f"{unique_id}.ogg"

    try:
        # Generate MP3 from Edge TTS
        communicate = edge_tts.Communicate(text=text, voice=voice_name)
        await communicate.save(str(mp3_path))

        # Convert MP3 to OGG/Opus using pydub + ffmpeg
        audio = AudioSegment.from_file(mp3_path)
        audio.export(ogg_path, format="ogg", codec="libopus")

        # Read final ogg bytes into memory
        with open(ogg_path, "rb") as f:
            ogg_bytes = f.read()

        return ogg_bytes

    finally:
        # Clean up temp files
        try:
            if mp3_path.exists():
                mp3_path.unlink()
        except Exception:
            pass

        try:
            if ogg_path.exists():
                ogg_path.unlink()
        except Exception:
            pass


# ============================================================
# COMMAND HANDLERS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start command handler.
    Sends introduction and opens settings keyboard.
    """
    user = update.effective_user
    prefs = get_user_preferences(user.id)

    language_label = LANGUAGE_VOICE_MAP[prefs["language"]]["label"]
    gender_label = prefs["gender"].capitalize()

    text = (
        f"Hello {user.first_name or 'there'}! 🎤\n\n"
        f"I am your Text-to-Speech bot.\n"
        f"Send me any text and I will convert it into a voice note.\n\n"
        f"Your current settings:\n"
        f"• Language: {language_label}\n"
        f"• Gender: {gender_label}\n\n"
        f"You can change your preferences below."
    )

    await update.message.reply_text(
        text,
        reply_markup=build_main_settings_keyboard(user.id)
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /settings command handler.
    """
    user_id = update.effective_user.id
    await update.message.reply_text(
        "Choose your preferred settings:",
        reply_markup=build_main_settings_keyboard(user_id)
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /help command handler.
    """
    help_text = (
        "How to use this bot:\n\n"
        "1. Use /settings to choose your language and voice gender.\n"
        "2. Send any text message.\n"
        "3. I will convert it into a voice note and send it back.\n\n"
        f"Limit: text must be under {MAX_TEXT_LENGTH} characters."
    )
    await update.message.reply_text(help_text)


# ============================================================
# CALLBACK QUERY HANDLER
# ============================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles all inline keyboard button presses.
    """
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    try:
        if data == "open_language_menu":
            await query.edit_message_text(
                "Select your preferred language:",
                reply_markup=build_language_keyboard()
            )
            return

        if data == "open_gender_menu":
            await query.edit_message_text(
                "Select your preferred voice gender:",
                reply_markup=build_gender_keyboard()
            )
            return

        if data == "back_to_settings":
            await query.edit_message_text(
                "Choose your preferred settings:",
                reply_markup=build_main_settings_keyboard(user_id)
            )
            return

        if data == "settings_done":
            prefs = get_user_preferences(user_id)
            language_label = LANGUAGE_VOICE_MAP[prefs["language"]]["label"]
            gender_label = prefs["gender"].capitalize()

            await query.edit_message_text(
                f"Settings saved ✅\n\n"
                f"Language: {language_label}\n"
                f"Gender: {gender_label}\n\n"
                f"Now send me any text."
            )
            return

        if data.startswith("set_language:"):
            language = data.split(":", 1)[1]

            if language in LANGUAGE_VOICE_MAP:
                update_user_preferences(user_id, language=language)

                prefs = get_user_preferences(user_id)
                language_label = LANGUAGE_VOICE_MAP[prefs["language"]]["label"]
                gender_label = prefs["gender"].capitalize()

                await query.edit_message_text(
                    f"Language updated ✅\n\n"
                    f"Language: {language_label}\n"
                    f"Gender: {gender_label}",
                    reply_markup=build_main_settings_keyboard(user_id)
                )
            else:
                await query.edit_message_text(
                    "Invalid language selection.",
                    reply_markup=build_main_settings_keyboard(user_id)
                )
            return

        if data.startswith("set_gender:"):
            gender = data.split(":", 1)[1]

            if gender in ("male", "female"):
                update_user_preferences(user_id, gender=gender)

                prefs = get_user_preferences(user_id)
                language_label = LANGUAGE_VOICE_MAP[prefs["language"]]["label"]
                gender_label = prefs["gender"].capitalize()

                await query.edit_message_text(
                    f"Gender updated ✅\n\n"
                    f"Language: {language_label}\n"
                    f"Gender: {gender_label}",
                    reply_markup=build_main_settings_keyboard(user_id)
                )
            else:
                await query.edit_message_text(
                    "Invalid gender selection.",
                    reply_markup=build_main_settings_keyboard(user_id)
                )
            return

    except Exception as e:
        logger.exception("Error handling callback query")
        await query.edit_message_text(f"An error occurred: {e}")


# ============================================================
# TEXT MESSAGE HANDLER
# ============================================================

async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles user text messages and converts them into a voice note.
    """
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()

    # Ignore commands in this text handler
    if text.startswith("/"):
        return

    if not text:
        await update.message.reply_text("Please send some text to convert.")
        return

    if len(text) > MAX_TEXT_LENGTH:
        await update.message.reply_text(
            f"Your text is too long.\n\n"
            f"Maximum allowed length is {MAX_TEXT_LENGTH} characters."
        )
        return

    language, gender, voice_name = get_voice_for_user(user_id)

    try:
        # Show typing/upload feedback
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)

        ogg_bytes = await generate_tts_ogg(text=text, voice_name=voice_name)

        voice_file = io.BytesIO(ogg_bytes)
        voice_file.name = "tts_voice.ogg"

        language_label = LANGUAGE_VOICE_MAP[language]["label"]

        await update.message.reply_voice(
            voice=voice_file,
            caption=f"Language: {language_label} | Gender: {gender.capitalize()}"
        )

    except Exception as e:
        logger.exception("TTS generation failed")
        await update.message.reply_text(
            "Sorry, I couldn't convert that text to speech right now. Please try again."
        )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global error handler to log unexpected exceptions.
    """
    logger.error("Exception while handling an update:", exc_info=context.error)


# ============================================================
# MAIN ENTRY
# ============================================================

def main() -> None:
    """
    Main function to start the Telegram bot.
    """
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is missing.")

    application = Application.builder().token(BOT_TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("help", help_command))

    # Button presses
    application.add_handler(CallbackQueryHandler(button_handler))

    # Text messages
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler)
    )

    # Global errors
    application.add_error_handler(error_handler)

    logger.info("Bot is starting...")
    application.run_polling()


if __name__ == "__main__":
    main()
