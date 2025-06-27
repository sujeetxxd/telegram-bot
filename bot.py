import logging
import json
import os
import subprocess
import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError

# --- Configuration ---
BOT_TOKEN = "8050233284:AAHyy5CwuIZo5_A7jIYEPgzTrmqd5G5bF5o"  # Your Bot Token
XERCESE_USER_ID = 7867584782      # Your (Xercese) User ID
CONFIG_FILE = 'bot_config.json' # Updated config file name
BOT_APP_CONFIG = {}             # Global variable to hold the loaded config
DELAY_BOT_SEND = 1.5  # Seconds between bot sending each item to end-user

# --- Logging ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Config Loading ---
def load_bot_config():
    global BOT_APP_CONFIG
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            BOT_APP_CONFIG = json.load(f)
        if 'menus' not in BOT_APP_CONFIG or 'actions' not in BOT_APP_CONFIG:
            logger.error(f"'{CONFIG_FILE}' is missing 'menus' or 'actions' top-level keys.")
            BOT_APP_CONFIG = {} # Reset to empty if structure is wrong
        else:
            logger.info(f"Loaded bot configuration from '{CONFIG_FILE}'.")
    except Exception as e:
        logger.error(f"Error loading '{CONFIG_FILE}': {e}")
        BOT_APP_CONFIG = {}

# --- Helper function to generate keyboards ---
def generate_keyboard_for_menu(menu_id: str) -> InlineKeyboardMarkup:
    menu_items = BOT_APP_CONFIG.get('menus', {}).get(menu_id, [])
    
    if not menu_items and menu_id != "root": # If a submenu is empty, provide a way back
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="navigate:root")]])
    elif not menu_items and menu_id == "root": # If root menu is empty (config error)
         return InlineKeyboardMarkup([[InlineKeyboardButton("Configuration error.", callback_data="noop")]])


    keyboard = []
    for item in menu_items:
        keyboard.append([InlineKeyboardButton(item["button_label"], callback_data=item["callback_data"])])
    return InlineKeyboardMarkup(keyboard)

# --- Start Command ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    logger.info(f"User {user.username} (ID: {user.id}) started the bot.")
    
    if not BOT_APP_CONFIG or 'root' not in BOT_APP_CONFIG.get('menus', {}):
        await update.message.reply_text("Bot configuration is incomplete or the main menu is missing. Please contact the admin.")
        return

    keyboard = generate_keyboard_for_menu("root")
    await update.message.reply_html(
        rf"Hi {user.mention_html()}! Please choose an option:",
        reply_markup=keyboard
    )

# --- Button Handler ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()  # Acknowledge callback
    
    callback_data_str = query.data
    if callback_data_str == "noop": # For placeholder buttons like config error
        return

    user = query.from_user
    end_user_chat_id = str(query.message.chat_id)

    logger.info(f"User {user.id} (ChatID: {end_user_chat_id}) clicked '{callback_data_str}'.")

    try:
        action_type, key = callback_data_str.split(":", 1)
    except ValueError:
        logger.warning(f"Invalid callback_data format: {callback_data_str}")
        try:
            await query.edit_message_text(text="⚠️ Invalid option selected.")
        except TelegramError as e:
            logger.error(f"Error editing message for invalid callback: {e}")
            await context.bot.send_message(chat_id=end_user_chat_id, text="⚠️ Invalid option selected.")
        return

    if action_type == "navigate":
        menu_display_name = key.replace("_submenu", "").replace("_", " ").title()
        if key == "root":
            menu_display_name = "Main Menu"
        
        new_keyboard = generate_keyboard_for_menu(key)
        try:
            await query.edit_message_text(
                text=f"📜 {menu_display_name}\nSelect an option:",
                reply_markup=new_keyboard,
                parse_mode='HTML'
            )
        except TelegramError as e:
            if "Message is not modified" in str(e):
                logger.info(f"Message not modified for navigation to {key}, likely same menu or no change.")
            else:
                logger.error(f"Error editing message for navigation to {key}: {e}")
                try:
                    await context.bot.send_message(chat_id=end_user_chat_id, text=f"Opened {menu_display_name}.", reply_markup=new_keyboard)
                except TelegramError as send_e: # Log if sending new message also fails
                    logger.error(f"Also failed to send new message for navigation: {send_e}")


    elif action_type == "action":
        action_config = BOT_APP_CONFIG.get('actions', {}).get(key)

        if not action_config:
            logger.error(f"Action key '{key}' not found in configuration.")
            await query.edit_message_text(text="⚠️ Selected action is not configured correctly.")
            return
        
        current_button_label = action_config.get("button_label", "the requested content")
        context.bot_data[f"last_button_for_{end_user_chat_id}"] = current_button_label

        bot_info = await context.bot.get_me()
        bot_username = bot_info.username
        if not bot_username:
            logger.error("Bot requires a username for this operation!")
            await query.edit_message_text(text="⚠️ Bot error: I need a username for this feature.")
            return
        
        try:
            await query.edit_message_text(text=f"Checking...")
        except TelegramError as e: 
            logger.warning(f"Could not edit message for request status, sending new one: {e}")
            await context.bot.send_message(chat_id=end_user_chat_id, text=f"⏳ Requesting: {current_button_label}...")


        private_channel_id = action_config["private_channel_id"]
        messages_identifier = action_config["messages_identifier"]
        
        logger.info(f"Calling forwarder.py: Chan='{private_channel_id}', IDN='{messages_identifier}', TargetBot='@{bot_username}', ForUser='{end_user_chat_id}'")

        try:
            forwarder_script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'forwarder.py')
            process = subprocess.run(
                [os.sys.executable, forwarder_script_path, 
                 private_channel_id, messages_identifier, bot_username, end_user_chat_id],
                capture_output=True, text=True, check=False, timeout=600 
            )
            
            stdout_data = process.stdout.strip()
            stderr_data = process.stderr.strip()
            logger.info(f"Forwarder stdout: {stdout_data}")
            if stderr_data: logger.error(f"Forwarder stderr: {stderr_data}")

            user_facing_message = f"checking..."
            try:
                response_json = json.loads(stdout_data)
                if isinstance(response_json, dict):
                    message_from_fwd = response_json.get("message", "No specific status.")
                    status = response_json.get("status", "unknown")
                    if status == "error":
                        user_facing_message = f"⚠️ Error from helper script: {message_from_fwd[:200]}"
                    elif "count_sent_to_bot" in response_json:
                        sent_to_bot = response_json["count_sent_to_bot"]
                        total_found = response_json.get("total_found", sent_to_bot) 
                        if total_found == 0:
                            user_facing_message = f"ℹ️ No messages found by helper for '{current_button_label}'."
                        else:
                            user_facing_message = f"Processing..."
                    else:
                        user_facing_message = f"Helper script status: {message_from_fwd}"
            except json.JSONDecodeError:
                user_facing_message = "Helper script finished, but status unclear. Waiting for items..."
                if process.returncode !=0:
                     output_to_show = stderr_data if stderr_data else stdout_data
                     if not output_to_show: output_to_show = "Unknown error from helper script."
                     user_facing_message = f"⚠️ Error processing request: {output_to_show[:300]}"
            
            try:
                await query.edit_message_text(text=user_facing_message)
            except TelegramError:
                await context.bot.send_message(chat_id=end_user_chat_id, text=user_facing_message)

        except subprocess.TimeoutExpired:
            logger.error(f"Forwarder script timed out for action '{key}'.")
            await context.bot.send_message(chat_id=end_user_chat_id, text=f"⏳ Helper script for '{current_button_label}' timed out.")
        except Exception as e:
            logger.error(f"Error in button_handler for action {key}: {e}", exc_info=True)
            await context.bot.send_message(chat_id=end_user_chat_id, text="🚨 Unexpected bot error processing your request.")
    else:
        logger.warning(f"Unknown action_type: {action_type} from callback_data: {callback_data_str}")
        try:
            await query.edit_message_text(text="⚠️ Unknown action type.")
        except TelegramError:
             await context.bot.send_message(chat_id=end_user_chat_id, text="⚠️ Unknown action type.")

async def xercese_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    text = message.text
    
    logger.info(f"Bot received message from Xercese: '{text[:100] if text else '<NoText_LikelyMedia>'}'")

    if text and text.startswith("CONTROL_TASK_START:"):
        try:
            parts = text.split(':', 2) 
            if len(parts) < 3:
                logger.error(f"Invalid CONTROL_TASK_START format: {text}")
                return
            requester_chat_id = int(parts[1])
            expected_items = int(parts[2])
            task_key = f"forward_task_{requester_chat_id}"
            
            button_label_for_task = context.bot_data.get(f"last_button_for_{requester_chat_id}", "the requested content")

            context.bot_data[task_key] = {
                'requester_chat_id': requester_chat_id,
                'expected_items': expected_items,
                'relayed_count': 0,
                'last_button_label': button_label_for_task
            }
            logger.info(f"Initialized task via CONTROL_MSG for {requester_chat_id} ('{button_label_for_task}'): Expecting {expected_items} items.")
            return 
        except Exception as e:
            logger.error(f"Error parsing CONTROL_TASK_START '{text}': {e}")
            return

    if text and text.startswith("CONTROL_TASK_END:"):
        try:
            parts = text.split(':', 2) 
            if len(parts) < 3:
                logger.error(f"Invalid CONTROL_TASK_END format: {text}")
                return
            requester_chat_id = int(parts[1])
            forwarded_by_xercese = int(parts[2])
            task_key = f"forward_task_{requester_chat_id}"
            
            if task_key in context.bot_data:
                task = context.bot_data[task_key]
                button_label = task.get('last_button_label', 'the content') 
                relayed_count = task.get('relayed_count',0)
                expected_items_for_task = task.get('expected_items', 0)

                logger.info(f"Received CONTROL_TASK_END for {requester_chat_id} ('{button_label}'). Xercese forwarded {forwarded_by_xercese}. Bot relayed {relayed_count}.")
                
                if expected_items_for_task > 0 and relayed_count < expected_items_for_task:
                    logger.warning(f"Task for {requester_chat_id} ('{button_label}') ended, but relayed count {relayed_count} < expected {expected_items_for_task}")
                    await context.bot.send_message(chat_id=requester_chat_id, text=f"Successfuly Sent ✔️")
                elif expected_items_for_task == 0 and forwarded_by_xercese == 0 : 
                     pass
                elif relayed_count == expected_items_for_task and expected_items_for_task >= 0 : # Also handle expected_items == 0 correctly
                    completion_message = f"✅ All items for '{button_label}' should now be with you. Task complete."
                    if expected_items_for_task == 0 : # Slightly different message if no items were expected/found
                        completion_message = f"✅ Task for '{button_label}' complete (no items were processed)."
                    # Check if message was already sent by relay loop
                    if not (task.get('relayed_count', 0) == expected_items_for_task and expected_items_for_task > 0 and task.get('_completed_message_sent_by_relay')):
                        await context.bot.send_message(chat_id=requester_chat_id, text=completion_message)
                
                del context.bot_data[task_key] 
                if f"last_button_for_{requester_chat_id}" in context.bot_data:
                    del context.bot_data[f"last_button_for_{requester_chat_id}"]
            else:
                logger.warning(f"Received CONTROL_TASK_END for {requester_chat_id} but no active task found (or already cleared).")
            return 
        except Exception as e:
            logger.error(f"Error parsing CONTROL_TASK_END '{text}': {e}")
            return

    active_task_key_found = None
    task_info_for_relay = None
    
    for t_key, t_val in context.bot_data.items():
        if t_key.startswith("forward_task_") and isinstance(t_val, dict):
            if t_val.get("expected_items", 0) > t_val.get("relayed_count", 0):
                task_info_for_relay = t_val
                active_task_key_found = t_key
                break 

    if not task_info_for_relay:
        logger.warning(f"Bot received content from Xercese but no suitable active task found. Ignoring message. Text: '{text[:50] if text else '<Media>'}'")
        return

    requester_chat_id_for_relay = task_info_for_relay['requester_chat_id']
    button_label_for_relay = task_info_for_relay.get('last_button_label', 'content')
    expected_items_for_relay = task_info_for_relay.get('expected_items', 0)

    sent_item_by_bot = False
    try:
        caption_to_use = None
        if message.caption:
            caption_to_use = message.caption
        elif message.text and not (message.video or message.document or message.photo or message.audio or message.voice):
            caption_to_use = message.text 

        if message.video:
            await context.bot.send_video(chat_id=requester_chat_id_for_relay, video=message.video.file_id, caption=caption_to_use)
            sent_item_by_bot = True
        elif message.document: 
            await context.bot.send_document(chat_id=requester_chat_id_for_relay, document=message.document.file_id, caption=caption_to_use)
            sent_item_by_bot = True
        elif message.photo:
            await context.bot.send_photo(chat_id=requester_chat_id_for_relay, photo=message.photo[-1].file_id, caption=caption_to_use)
            sent_item_by_bot = True
        elif message.text: 
            await context.bot.send_message(chat_id=requester_chat_id_for_relay, text=message.text) 
            sent_item_by_bot = True
        
        if sent_item_by_bot:
            task_info_for_relay['relayed_count'] += 1
            logger.info(f"Bot relayed item {task_info_for_relay['relayed_count']}/{expected_items_for_relay} for task '{button_label_for_relay}' (Key: {active_task_key_found}) to {requester_chat_id_for_relay}")
            
            if expected_items_for_relay >= 0 and task_info_for_relay['relayed_count'] >= expected_items_for_relay: # Handle expected_items=0
                logger.info(f"All {expected_items_for_relay} items relayed for task '{button_label_for_relay}'. Clearing task.")
                if expected_items_for_relay > 0 : # Only send completion if items were expected and relayed
                    await context.bot.send_message(chat_id=requester_chat_id_for_relay, text=f"✅ All items for '{button_label_for_relay}' have been sent to you.")
                    task_info_for_relay['_completed_message_sent_by_relay'] = True # Mark that this message was sent
                elif expected_items_for_relay == 0: # No items were expected to begin with
                    # Message already handled by button_handler or CONTROL_TASK_END
                    pass

                del context.bot_data[active_task_key_found]
                if f"last_button_for_{requester_chat_id_for_relay}" in context.bot_data:
                    del context.bot_data[f"last_button_for_{requester_chat_id_for_relay}"]
            await asyncio.sleep(DELAY_BOT_SEND)

    except TelegramError as e:
        logger.error(f"BOT_ERROR: Failed to relay message from Xercese to {requester_chat_id_for_relay}. Error: {e}")
        await context.bot.send_message(chat_id=requester_chat_id_for_relay, text=f"⚠️ Error relaying one item for '{button_label_for_relay}': {e}")
        if active_task_key_found and active_task_key_found in context.bot_data: del context.bot_data[active_task_key_found] 
        if f"last_button_for_{requester_chat_id_for_relay}" in context.bot_data: del context.bot_data[f"last_button_for_{requester_chat_id_for_relay}"]
    except Exception as e_relay:
        logger.error(f"BOT_ERROR: Unexpected error relaying message from Xercese: {e_relay}", exc_info=True)
        await context.bot.send_message(chat_id=requester_chat_id_for_relay, text=f"⚠️ An unexpected error occurred while relaying an item for '{button_label_for_relay}'.")
        if active_task_key_found and active_task_key_found in context.bot_data: del context.bot_data[active_task_key_found]
        if f"last_button_for_{requester_chat_id_for_relay}" in context.bot_data: del context.bot_data[f"last_button_for_{requester_chat_id_for_relay}"]

def main() -> None:
    load_bot_config() 
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN": 
        logger.critical("BOT_TOKEN not set!")
        return
    if not XERCESE_USER_ID: 
        logger.critical("XERCESE_USER_ID not set!")
        return
    if not BOT_APP_CONFIG:
        logger.critical(f"Bot configuration from '{CONFIG_FILE}' failed to load or is empty. Bot cannot start.")
        return

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(button_handler)) 
    
    xercese_filter = filters.User(user_id=XERCESE_USER_ID)
    application.add_handler(MessageHandler(xercese_filter & (~filters.COMMAND), xercese_message_handler))

    logger.info("Bot starting with new menu structure...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
