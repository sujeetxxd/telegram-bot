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
CONFIG_FILE = 'bot_config.json'
BOT_APP_CONFIG = {}
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
            BOT_APP_CONFIG = {}
        else:
            logger.info(f"Loaded bot configuration from '{CONFIG_FILE}'.")
    except Exception as e:
        logger.error(f"Error loading '{CONFIG_FILE}': {e}")
        BOT_APP_CONFIG = {}

# --- Helper function to find the parent menu of an action ---
def find_parent_menu_for_action(action_key_to_find: str) -> str:
    if not BOT_APP_CONFIG or 'menus' not in BOT_APP_CONFIG:
        return "root"
    for menu_name, menu_items in BOT_APP_CONFIG.get('menus', {}).items():
        if isinstance(menu_items, list):
            for item in menu_items:
                if isinstance(item, dict) and item.get("callback_data") == f"action:{action_key_to_find}":
                    return menu_name
    return "root"

# --- Helper function to generate keyboards ---
def generate_keyboard_for_menu(menu_id: str) -> InlineKeyboardMarkup:
    menu_items = BOT_APP_CONFIG.get('menus', {}).get(menu_id, [])
    if not menu_items and menu_id != "root":
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="navigate:root")]])
    elif not menu_items and menu_id == "root":
         return InlineKeyboardMarkup([[InlineKeyboardButton("Configuration error.", callback_data="noop")]])
    keyboard = []
    for item in menu_items:
        keyboard.append([InlineKeyboardButton(item["button_label"], callback_data=item["callback_data"])])
    return InlineKeyboardMarkup(keyboard)

# --- Start Command ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id
    logger.info(f"User {user.username} (ID: {user.id}, ChatID: {chat_id}) started the bot with /start or /menu.")
    
    load_bot_config()
    if not BOT_APP_CONFIG or 'root' not in BOT_APP_CONFIG.get('menus', {}):
        await update.message.reply_text("Bot configuration is incomplete or the main menu is missing. Please contact the admin.")
        return

    # Try to delete the previous main menu message
    if 'last_main_menu_message_id' in context.user_data:
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=context.user_data['last_main_menu_message_id']
            )
            logger.info(f"Deleted previous main menu message {context.user_data['last_main_menu_message_id']} for user {user.id}")
        except TelegramError as e:
            logger.warning(f"Could not delete previous main menu message for user {user.id}: {e}")
        finally:
            del context.user_data['last_main_menu_message_id']

    context.user_data['last_navigated_menu'] = "root"
    keyboard = generate_keyboard_for_menu("root")
    
    sent_message = await update.message.reply_html(
        rf"Hi {user.mention_html()}! Please choose an option:",
        reply_markup=keyboard
    )
    context.user_data['last_main_menu_message_id'] = sent_message.message_id
    logger.info(f"Sent new main menu message {sent_message.message_id} to user {user.id}")

# --- Button Handler ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    callback_data_str = query.data
    if callback_data_str == "noop":
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
        menu_display_name = key.replace("_submenu", "").replace("_menu","").replace("_", " ").title()
        if key == "root":
            menu_display_name = "Main Menu"
        
        new_keyboard = generate_keyboard_for_menu(key)
        try:
            await query.edit_message_text(
                text=f"📜 {menu_display_name}\nSelect an option:",
                reply_markup=new_keyboard,
                parse_mode='HTML'
            )
            context.user_data['last_navigated_menu'] = key
            if key == "root" and query.message: # If navigating to root, this becomes the new main menu message
                 context.user_data['last_main_menu_message_id'] = query.message.message_id
        except TelegramError as e:
            if "Message is not modified" in str(e):
                logger.info(f"Message not modified for navigation to {key}.")
            else:
                logger.error(f"Error editing message for navigation to {key}: {e}")
                try:
                    # If edit fails, send a new message (this might happen if original message is too old)
                    # If sending a new root menu, it should also be tracked.
                    sent_message = await context.bot.send_message(chat_id=end_user_chat_id, text=f"Opened {menu_display_name}.", reply_markup=new_keyboard)
                    context.user_data['last_navigated_menu'] = key
                    if key == "root":
                        # If previous main menu was deleted by /start, this new one becomes the tracked one
                        if 'last_main_menu_message_id' in context.user_data: # Check if /start already deleted one
                            try:
                                await context.bot.delete_message(chat_id=end_user_chat_id, message_id=context.user_data['last_main_menu_message_id'])
                            except: pass # Ignore if already deleted or error
                        context.user_data['last_main_menu_message_id'] = sent_message.message_id
                except TelegramError as send_e:
                    logger.error(f"Also failed to send new message for navigation: {send_e}")

    elif action_type == "action":
        action_config = BOT_APP_CONFIG.get('actions', {}).get(key)
        current_button_label = "the requested content"
        if action_config:
            current_button_label = action_config.get("button_label", current_button_label)
        else:
            logger.error(f"Action key '{key}' not found in configuration early.")
            await query.edit_message_text(text="⚠️ Selected action is not configured correctly.")
            return
        
        parent_menu_of_action = find_parent_menu_for_action(key)
        context.user_data[f'parent_menu_for_action_{key}'] = parent_menu_of_action
        context.user_data['last_navigated_menu'] = parent_menu_of_action

        context.bot_data[f"last_button_for_{end_user_chat_id}"] = current_button_label
        
        # Store current message details for potential later edit by Xercese handler
        task_key = f"forward_task_{end_user_chat_id}" # This task_key is used in xercese_message_handler
        if task_key not in context.bot_data: context.bot_data[task_key] = {}
        if query.message:
            context.bot_data[task_key]['status_message_id'] = query.message.message_id
            context.bot_data[task_key]['status_chat_id'] = query.message.chat_id
        else: # Should not happen for callback queries
            context.bot_data[task_key]['status_message_id'] = None
            context.bot_data[task_key]['status_chat_id'] = int(end_user_chat_id)


        content_delivery_method = action_config.get("delivery_method")

        if content_delivery_method == "file_id":
            file_id = action_config.get("file_id")
            caption = action_config.get("caption")
            file_type = action_config.get("file_type", "document").lower()

            logger.info(f"Attempting file_id delivery for action: {key}, file_type: {file_type}, file_id: '{file_id}'")

            if not file_id:
                logger.error(f"Action {key} configured for file_id delivery but no file_id provided.")
                try:
                    await query.edit_message_text(text="⚠️ Content configuration error (missing file ID).")
                except TelegramError:
                    await context.bot.send_message(chat_id=end_user_chat_id, text="⚠️ Content configuration error (missing file ID).")
                return

            initial_status_message = f"⏳ Preparing: {current_button_label}..."
            try:
                await query.edit_message_text(text=initial_status_message)
            except TelegramError as e:
                logger.warning(f"Could not edit message for file_id request status, sending new one: {e}")
                sent_status_msg = await context.bot.send_message(chat_id=end_user_chat_id, text=initial_status_message)
                # Update stored status message ID if a new one was sent
                context.bot_data[task_key]['status_message_id'] = sent_status_msg.message_id
                context.bot_data[task_key]['status_chat_id'] = sent_status_msg.chat_id


            try:
                if file_type == "video":
                    await context.bot.send_video(chat_id=end_user_chat_id, video=file_id, caption=caption)
                elif file_type == "document":
                    await context.bot.send_document(chat_id=end_user_chat_id, document=file_id, caption=caption)
                elif file_type == "photo":
                    await context.bot.send_photo(chat_id=end_user_chat_id, photo=file_id, caption=caption)
                elif file_type == "audio":
                    await context.bot.send_audio(chat_id=end_user_chat_id, audio=file_id, caption=caption)
                else:
                    logger.error(f"Unsupported file_type '{file_type}' for file_id delivery in action {key}.")
                    await query.edit_message_text(text=f"⚠️ Unsupported content type for '{current_button_label}'.")
                    return
                
                logger.info(f"Successfully initiated sending of file_id {file_id} for action {key} to chat {end_user_chat_id}.")
                status_msg_id = context.bot_data[task_key].get('status_message_id')
                status_chat_id_for_edit = context.bot_data[task_key].get('status_chat_id')
                if status_msg_id and status_chat_id_for_edit:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=status_chat_id_for_edit, 
                            message_id=status_msg_id,
                            text=f"✅ Content for '{current_button_label}' sent!", 
                            reply_markup=None # Remove keyboard
                        )
                    except TelegramError as e_edit_final:
                         logger.warning(f"Could not edit status message to final for {key} after sending file_id: {e_edit_final}")
                # Clean up task specific data for file_id delivery as it's a one-shot
                if task_key in context.bot_data: del context.bot_data[task_key]
                if f"last_button_for_{end_user_chat_id}" in context.bot_data: del context.bot_data[f"last_button_for_{end_user_chat_id}"]


            except TelegramError as e:
                logger.error(f"TelegramError sending file_id '{file_id}' (type: {file_type}) for action '{key}' to {end_user_chat_id}: {e}")
                error_message_to_user = f"⚠️ Sorry, error sending '{current_button_label}'."
                if "File id is invalid" in str(e) or "FILE_ID_INVALID" in str(e) or "wrong file identifier" in str(e).lower():
                    error_message_to_user = f"⚠️ Content ID for '{current_button_label}' is invalid. Please notify admin."
                elif "FILE_REFERENCE_EXPIRED" in str(e) or "file reference expired" in str(e).lower():
                     error_message_to_user = f"⚠️ File reference for '{current_button_label}' expired. Please ask admin to refresh."
                
                status_msg_id = context.bot_data[task_key].get('status_message_id')
                status_chat_id_for_edit = context.bot_data[task_key].get('status_chat_id')
                if status_msg_id and status_chat_id_for_edit:
                    try: await query.edit_message_text(text=error_message_to_user)
                    except TelegramError: await context.bot.send_message(chat_id=end_user_chat_id, text=error_message_to_user)
                else:
                    await context.bot.send_message(chat_id=end_user_chat_id, text=error_message_to_user)
            return 

        # --- Logic for forwarder.py ---
        bot_info = await context.bot.get_me()
        bot_username = bot_info.username
        if not bot_username: # Should have been checked earlier too
            logger.error("Bot requires a username for this operation!")
            await query.edit_message_text(text="⚠️ Bot error: I need a username for this feature.")
            return

        initial_status_message_fwd = f"⏳ Requesting: {current_button_label}...\nSending, please wait..."
        try:
            await query.edit_message_text(text=initial_status_message_fwd)
        except TelegramError as e: 
            logger.warning(f"Could not edit message for forwarder request status, sending new one: {e}")
            sent_status_msg_fwd = await context.bot.send_message(chat_id=end_user_chat_id, text=initial_status_message_fwd)
            context.bot_data[task_key]['status_message_id'] = sent_status_msg_fwd.message_id # Update stored ID
            context.bot_data[task_key]['status_chat_id'] = sent_status_msg_fwd.chat_id


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

            user_facing_message = f"Task for '{current_button_label}' has been processed by the helper script." 
            keyboard_to_send = None 

            try:
                response_json = json.loads(stdout_data)
                if isinstance(response_json, dict):
                    message_from_fwd = response_json.get("message", "No specific status.")
                    status = response_json.get("status", "unknown")
                    
                    if status == "error":
                        is_channel_not_found_error = "not found or inaccessible" in message_from_fwd.lower()
                        if is_channel_not_found_error:
                            user_facing_message = "Content not found?"
                            update_button = InlineKeyboardButton("Update", callback_data=f"request_update:{key}") 
                            keyboard_to_send = InlineKeyboardMarkup([[update_button]])
                        else:
                            user_facing_message = f"⚠️ Error from helper script: {message_from_fwd[:200]}"
                    # This message is only a status update, not the final success message. This part is OK.
                    elif "count_sent_to_bot" in response_json:
                        sent_to_bot = response_json["count_sent_to_bot"]
                        total_found = response_json.get("total_found", sent_to_bot) 
                        if total_found == 0:
                            user_facing_message = f"ℹ️ No messages found for '{current_button_label}'."
                        else:
                            user_facing_message = f"Transfer for '{current_button_label}' initiated. Please wait for all items to arrive..."
                    else: 
                        user_facing_message = f"Helper script status: {message_from_fwd}"
            except json.JSONDecodeError:
                user_facing_message = "Helper script finished, but status unclear. Waiting for items..."
                if process.returncode != 0: 
                     output_to_show = stderr_data if stderr_data else stdout_data
                     if not output_to_show: output_to_show = "Unknown error from helper script."
                     user_facing_message = f"⚠️ Error processing request: {output_to_show[:300]}"
            
            status_msg_id_fwd = context.bot_data[task_key].get('status_message_id')
            status_chat_id_fwd = context.bot_data[task_key].get('status_chat_id')
            # Don't edit if the message already indicates no items were found.
            # The control messages will handle the final status edit.
            if "No messages found" not in user_facing_message:
                if status_msg_id_fwd and status_chat_id_fwd:
                    try: await context.bot.edit_message_text(chat_id=status_chat_id_fwd, message_id=status_msg_id_fwd, text=user_facing_message, reply_markup=keyboard_to_send)
                    except TelegramError: await context.bot.send_message(chat_id=end_user_chat_id, text=user_facing_message, reply_markup=keyboard_to_send)
                else: # Fallback if somehow message ID wasn't tracked
                    await context.bot.send_message(chat_id=end_user_chat_id, text=user_facing_message, reply_markup=keyboard_to_send)


        except subprocess.TimeoutExpired:
            logger.error(f"Forwarder script timed out for action '{key}'.")
            await context.bot.send_message(chat_id=end_user_chat_id, text=f"⏳ Helper script for '{current_button_label}' timed out.")
        except Exception as e:
            logger.error(f"Error in button_handler for action {key}: {e}", exc_info=True)
            await context.bot.send_message(chat_id=end_user_chat_id, text="🚨 Unexpected bot error processing your request.")
    
    elif action_type == "request_update":
        original_action_key = key 
        logger.info(f"User {user.id} (ChatID: {end_user_chat_id}) requested an update for action '{original_action_key}'.")
        
        parent_menu_key = context.user_data.get(f'parent_menu_for_action_{original_action_key}')
        if not parent_menu_key:
            parent_menu_key = find_parent_menu_for_action(original_action_key)
            logger.warning(f"Parent menu for action {original_action_key} was not in user_data, found: {parent_menu_key} using helper.")
            
        back_button_callback = f"navigate:{parent_menu_key}"
        back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=back_button_callback)]])

        try:
            await query.edit_message_text(
                text="⏳ Updating... will get updated within 24 hours", 
                reply_markup=back_keyboard
            ) 
        except TelegramError as e:
            logger.error(f"Error editing message for update request: {e}")
            await context.bot.send_message(
                chat_id=end_user_chat_id, 
                text="⏳ Updating... will get updated within 24 hours",
                reply_markup=back_keyboard
            )
        
        action_details = BOT_APP_CONFIG.get('actions', {}).get(original_action_key, {})
        button_label_for_admin = action_details.get('button_label', 'N/A')
        admin_message = (
            f"📢 Content Update Request:\n"
            f"User: {user.first_name} (ID: {user.id}, Username: @{user.username if user.username else 'N/A'})\n"
            f"Requested update for: '{button_label_for_admin}' (Action Key: {original_action_key})\n"
            f"Parent menu for back button: {parent_menu_key}"
        )
        try:
            if XERCESE_USER_ID:
                 await context.bot.send_message(chat_id=XERCESE_USER_ID, text=admin_message)
                 logger.info(f"Sent update request notification to admin {XERCESE_USER_ID} for action '{original_action_key}'.")
        except Exception as e_admin:
            logger.error(f"Failed to send update request notification to admin: {e_admin}")

    else:
        logger.warning(f"Unknown action_type: {action_type} from callback_data: {callback_data_str}")
        try:
            await query.edit_message_text(text="⚠️ Unknown action type.")
        except TelegramError:
             await context.bot.send_message(chat_id=end_user_chat_id, text="⚠️ Unknown action type.")

async def finalize_task(context: ContextTypes.DEFAULT_TYPE, task_key: str):
    """Sends the final completion message and cleans up task data."""
    if task_key not in context.bot_data:
        return

    task = context.bot_data[task_key]
    button_label = task.get('last_button_label', 'the requested content')
    requester_chat_id = task.get('requester_chat_id')
    status_message_id = task.get('status_message_id')
    status_chat_id = task.get('status_chat_id')
    final_count = task.get('final_count_from_forwarder', 0)
    
    final_message_text = "Sent successfully ✅"

    if final_count == 0:
        final_message_text = f"ℹ️ No items were found for '{button_label}'."

    if status_message_id and status_chat_id:
        try:
            await context.bot.edit_message_text(
                text=final_message_text,
                chat_id=status_chat_id,
                message_id=status_message_id,
                reply_markup=None
            )
            logger.info(f"Edited status message for completed task {task_key}")
        except TelegramError as e:
            logger.warning(f"Could not edit status message for {task_key}, sending new one. Error: {e}")
            if requester_chat_id:
                await context.bot.send_message(chat_id=requester_chat_id, text=final_message_text)
    elif requester_chat_id:
        await context.bot.send_message(chat_id=requester_chat_id, text=final_message_text)

    # Cleanup
    if task_key in context.bot_data:
        del context.bot_data[task_key]
    if requester_chat_id and f"last_button_for_{requester_chat_id}" in context.bot_data:
        del context.bot_data[f"last_button_for_{requester_chat_id}"]
    logger.info(f"Cleaned up data for task {task_key}.")


async def xercese_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    text = message.text
    
    logger.info(f"Bot received message from Xercese: '{text[:100] if text else '<NoText_LikelyMedia>'}'")

    if text and (text.startswith("CONTROL_TASK_START:") or text.startswith("CONTROL_TASK_END:")):
        try:
            parts = text.split(':', 2) 
            if len(parts) < 3:
                logger.error(f"Invalid CONTROL message format: {text}")
                return
            control_type = parts[0]
            requester_chat_id_from_text = int(parts[1])
            payload_value = int(parts[2])
            task_key = f"forward_task_{requester_chat_id_from_text}"

            if control_type == "CONTROL_TASK_START":
                status_msg_id = context.bot_data.get(task_key, {}).get('status_message_id')
                status_chat_id = context.bot_data.get(task_key, {}).get('status_chat_id')

                context.bot_data[task_key] = {
                    'requester_chat_id': requester_chat_id_from_text,
                    'expected_items': payload_value,
                    'relayed_count': 0,
                    'last_button_label': context.bot_data.get(f"last_button_for_{requester_chat_id_from_text}", "the requested content"),
                    'status_message_id': status_msg_id,
                    'status_chat_id': status_chat_id,
                    'control_end_received': False,
                    'final_count_from_forwarder': -1,
                }
                logger.info(f"Initialized task {task_key} via CONTROL_START: Expecting {payload_value} items.")
                return

            elif control_type == "CONTROL_TASK_END":
                if task_key in context.bot_data:
                    task = context.bot_data[task_key]
                    task['control_end_received'] = True
                    task['final_count_from_forwarder'] = payload_value
                    
                    logger.info(f"Received CONTROL_END for {task_key}. Final count is {payload_value}. Bot has relayed {task.get('relayed_count', 0)}.")

                    if task.get('relayed_count', 0) >= payload_value:
                        logger.info(f"Task {task_key} already complete on CONTROL_END receipt. Finalizing.")
                        await finalize_task(context, task_key)
                else:
                    logger.warning(f"Received CONTROL_TASK_END for {requester_chat_id_from_text} but no active task found.")
                return
        except Exception as e:
            logger.error(f"Error parsing CONTROL message '{text}': {e}", exc_info=True)
            return

    # --- Message Relaying Logic ---
    active_task_key_found = None
    task_info_for_relay = None
    
    for t_key, t_val in context.bot_data.items():
        if t_key.startswith("forward_task_") and isinstance(t_val, dict):
            final_count = t_val.get('final_count_from_forwarder', -1)
            is_complete = t_val.get('control_end_received') and final_count != -1 and t_val.get('relayed_count', 0) >= final_count
            if not is_complete:
                task_info_for_relay = t_val
                active_task_key_found = t_key
                break

    if not task_info_for_relay:
        logger.warning(f"Bot received content from Xercese but no suitable active task found. Ignoring. Text: '{text[:50] if text else '<Media>'}'")
        return

    requester_chat_id_for_relay = task_info_for_relay['requester_chat_id']
    button_label_for_relay = task_info_for_relay.get('last_button_label', 'content')
    
    sent_item_by_bot = False
    try:
        caption_to_use = message.caption
        
        if message.video:
            await context.bot.send_video(chat_id=requester_chat_id_for_relay, video=message.video.file_id, caption=caption_to_use)
            sent_item_by_bot = True
        elif message.document: 
            await context.bot.send_document(chat_id=requester_chat_id_for_relay, document=message.document.file_id, caption=caption_to_use)
            sent_item_by_bot = True
        elif message.photo:
            await context.bot.send_photo(chat_id=requester_chat_id_for_relay, photo=message.photo[-1].file_id, caption=caption_to_use) 
            sent_item_by_bot = True
        elif message.audio:
            await context.bot.send_audio(chat_id=requester_chat_id_for_relay, audio=message.audio.file_id, caption=caption_to_use)
            sent_item_by_bot = True
        elif message.voice:
            await context.bot.send_voice(chat_id=requester_chat_id_for_relay, voice=message.voice.file_id, caption=caption_to_use) 
            sent_item_by_bot = True
        elif message.text: 
            await context.bot.send_message(chat_id=requester_chat_id_for_relay, text=message.text) 
            sent_item_by_bot = True
        else:
            logger.warning(f"Received message from Xercese with no standard content to relay: {message.to_dict()}")
        
        if sent_item_by_bot:
            task_info_for_relay['relayed_count'] += 1
            relayed_so_far = task_info_for_relay['relayed_count']
            expected_total = task_info_for_relay.get('expected_items', 'N/A')
            logger.info(f"Bot relayed item {relayed_so_far}/{expected_total} for task '{active_task_key_found}' to {requester_chat_id_for_relay}")
            
            # Wait AFTER sending each item
            await asyncio.sleep(DELAY_BOT_SEND)

            # Check for completion AFTER the delay
            is_end_signal_received = task_info_for_relay.get('control_end_received', False)
            final_count = task_info_for_relay.get('final_count_from_forwarder', -1)

            if is_end_signal_received and final_count != -1 and relayed_so_far >= final_count:
                logger.info(f"Relay loop completed task {active_task_key_found}. Finalizing.")
                await finalize_task(context, active_task_key_found)

    except TelegramError as e:
        logger.error(f"BOT_ERROR: Failed to relay message from Xercese to {requester_chat_id_for_relay}. Error: {e}")
        await context.bot.send_message(chat_id=requester_chat_id_for_relay, text=f"⚠️ Error relaying one item for '{button_label_for_relay}': {e}")
    except Exception as e_relay:
        logger.error(f"BOT_ERROR: Unexpected error relaying message from Xercese: {e_relay}", exc_info=True)
        await context.bot.send_message(chat_id=requester_chat_id_for_relay, text=f"⚠️ An unexpected error occurred while relaying an item for '{button_label_for_relay}'.")


def main() -> None:
    load_bot_config() 
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN": 
        logger.critical("BOT_TOKEN not set or is placeholder!")
        return 
    if not XERCESE_USER_ID: 
        logger.critical("XERCESE_USER_ID not set!")
        return
    if not BOT_APP_CONFIG:
        logger.critical(f"Bot configuration from '{CONFIG_FILE}' failed to load or is empty. Bot cannot start.")
        return

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler(["start", "menu"], start_command)) 
    application.add_handler(CallbackQueryHandler(button_handler)) 
    
    xercese_filter = filters.User(user_id=XERCESE_USER_ID)
    application.add_handler(MessageHandler(xercese_filter & (~filters.COMMAND), xercese_message_handler))

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
