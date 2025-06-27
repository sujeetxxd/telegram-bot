import asyncio
import sys
import logging
import os
import json 
from telethon import TelegramClient
from telethon.tl.types import PeerChannel
from telethon.errors import FloodWaitError # Add other relevant Telethon errors as needed

# --- Configuration ---
API_ID = 24359727 # Replace
API_HASH = "d48bd9d9b06a1d46b46cc169a5a8a42c"  # Replace
SESSION_NAME = "my_user_session"

# --- Logging ---
log_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'forwarder.log')
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", handlers=[logging.FileHandler(log_file_path, encoding='utf-8')])
logger = logging.getLogger(__name__)

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# --- Constants ---
FETCH_ALL_VIDEOS_FILES = "__ALL_VIDEOS_FILES__"
FETCH_EVERYTHING = "__FETCH_EVERYTHING__"
FORWARD_BATCH_SIZE = 95 # As per Telegram limits, max 100 per forward_messages
DELAY_BETWEEN_BATCHES = 2.0 # Seconds

async def get_message_ids_for_forwarding(channel_entity, identifier_str):
    message_ids = []
    logger.info(f"Getting message IDs from channel {getattr(channel_entity, 'id', channel_entity)} using identifier '{identifier_str}'")
    
    if identifier_str == FETCH_EVERYTHING:
        async for message in client.iter_messages(channel_entity):
            message_ids.append(message.id)
    elif identifier_str == FETCH_ALL_VIDEOS_FILES:
        async for message in client.iter_messages(channel_entity):
            if message.video or (message.document and not message.sticker and not message.audio and not message.voice):
                message_ids.append(message.id)
    elif all(part.isdigit() for part in identifier_str.split()): # Space-separated list of message IDs
        message_ids = [int(mid) for mid in identifier_str.split()]
    else: # Treat as a search query
        search_limit = 100 # How many matching messages to fetch for a search
        temp_ids = []
        # Iter_messages with search can be slow if iterating the whole channel history.
        # If channel is very large, consider limits or other search strategies.
        async for message in client.iter_messages(channel_entity, search=identifier_str, limit=search_limit * 5): # Iterate more to find up to search_limit
            temp_ids.append(message.id)
            if len(temp_ids) >= search_limit:
                break
        message_ids = temp_ids

    if message_ids: # Forward in chronological order (oldest first)
        message_ids.reverse() 
    logger.info(f"Found {len(message_ids)} message IDs for identifier '{identifier_str}'.")
    return message_ids

async def forward_content_to_bot(target_bot_peer, message_ids_to_forward, from_channel_entity):
    total_forwarded_successfully = 0
    if not message_ids_to_forward:
        return 0

    logger.info(f"Attempting to forward {len(message_ids_to_forward)} messages to bot {getattr(target_bot_peer, 'id', target_bot_peer)}")
    
    for i in range(0, len(message_ids_to_forward), FORWARD_BATCH_SIZE):
        batch_ids = message_ids_to_forward[i:i+FORWARD_BATCH_SIZE]
        retries = 2
        for attempt in range(retries):
            try:
                await client.forward_messages(entity=target_bot_peer, messages=batch_ids, from_peer=from_channel_entity)
                total_forwarded_successfully += len(batch_ids)
                logger.info(f"Successfully forwarded batch of {len(batch_ids)} messages. Total forwarded: {total_forwarded_successfully}")
                if i + FORWARD_BATCH_SIZE < len(message_ids_to_forward): # If there are more batches
                    await asyncio.sleep(DELAY_BETWEEN_BATCHES)
                break # Success, exit retry loop for this batch
            except FloodWaitError as fwe:
                logger.warning(f"FloodWaitError for {fwe.seconds}s when forwarding batch. Attempt {attempt + 1}/{retries}. Sleeping.")
                if fwe.seconds > 300: # If wait time is too long, maybe stop or log significantly
                    logger.error(f"Flood wait time {fwe.seconds}s is too long. Aborting this batch after current sleep.")
                    await asyncio.sleep(fwe.seconds + 5) # Sleep at least requested time
                    # Not breaking outer loop, just this batch might fail more if it retries into another flood.
                    # Consider how to handle catastrophic flood waits.
                    return total_forwarded_successfully # Or raise to stop all
                await asyncio.sleep(fwe.seconds + 5) # Sleep and retry
            except Exception as e:
                logger.error(f"Error forwarding batch (IDs: {batch_ids[:3]}...) to bot: {e}. Attempt {attempt + 1}/{retries}.", exc_info=True)
                if attempt < retries - 1:
                    await asyncio.sleep(5) # Wait before retrying
                else:
                    logger.error(f"Failed to forward batch after {retries} attempts. Skipping this batch.")
                    # Optionally, try to forward messages one by one from this failed batch,
                    # but that adds complexity. For now, skip the batch.
    return total_forwarded_successfully


async def main_logic(private_channel_identifier, messages_identifier_str, target_bot_username_str, original_requester_chat_id_str):
    count_forwarded_to_bot = 0
    expected_count = 0
    try:
        if not client.is_connected():
            await client.connect()
        if not await client.is_user_authorized():
            logger.info("User is not authorized. Attempting to start client (login).")
            await client.start() # This will prompt for phone/code if first time or session expired
            if not await client.is_user_authorized():
                logger.error("CRITICAL: User authorization failed after attempting start(). Cannot proceed.")
                print(json.dumps({"status": "error", "message": "Telethon client authorization failed.", "count_sent_to_bot": 0, "total_found": 0}))
                return False
        
        logger.info("Client connected and authorized.")
        
        try:
            from_channel_entity = await client.get_entity(private_channel_identifier)
        except ValueError as ve:
            logger.error(f"Could not find/access the source channel: {private_channel_identifier}. Error: {ve}")
            print(json.dumps({"status": "error", "message": f"Source channel '{private_channel_identifier}' not found or inaccessible.", "count_sent_to_bot": 0, "total_found": 0}))
            return False
            
        target_bot_peer = await client.get_entity(target_bot_username_str)
        logger.info(f"Source channel entity: {getattr(from_channel_entity, 'id', from_channel_entity)}, Target bot entity: {getattr(target_bot_peer, 'id', target_bot_peer)}")

        message_ids = await get_message_ids_for_forwarding(from_channel_entity, messages_identifier_str)
        expected_count = len(message_ids)

        if not message_ids:
            logger.info("No messages found in source channel matching the criteria.")
            # Bot.py's button_handler will inform the user based on total_found: 0
            # Send control message to bot indicating no messages START (0 items)
            control_message_start_empty = f"CONTROL_TASK_START:{original_requester_chat_id_str}:0"
            await client.send_message(target_bot_peer, control_message_start_empty)
            # And control message END (0 items)
            control_message_end_empty = f"CONTROL_TASK_END:{original_requester_chat_id_str}:0"
            await client.send_message(target_bot_peer, control_message_end_empty)
            print(json.dumps({"status": "success", "message": "No messages found.", "count_sent_to_bot": 0, "total_found": 0}))
            return True

        # 1. Send control message to bot with original requester's chat_id and expected count
        control_message_start = f"CONTROL_TASK_START:{original_requester_chat_id_str}:{expected_count}"
        await client.send_message(target_bot_peer, control_message_start)
        logger.info(f"Sent control message to bot: {control_message_start}")
        await asyncio.sleep(0.5) # Small delay to help bot process START before content

        # 2. Forward the actual content messages to the bot
        count_forwarded_to_bot = await forward_content_to_bot(target_bot_peer, message_ids, from_channel_entity)
        
        # 3. Send an end control message
        control_message_end = f"CONTROL_TASK_END:{original_requester_chat_id_str}:{count_forwarded_to_bot}" # Use actual count forwarded
        await client.send_message(target_bot_peer, control_message_end)
        logger.info(f"Sent end control message to bot: {control_message_end}")

        print(json.dumps({
            "status": "success", 
            "message": f"Task initiated: Forwarded {count_forwarded_to_bot}/{expected_count} items to bot for user {original_requester_chat_id_str}.", 
            "count_sent_to_bot": count_forwarded_to_bot, 
            "total_found": expected_count
        }))
        return True

    except Exception as e:
        logger.error(f"Error in forwarder main_logic: {e}", exc_info=True)
        # Try to send a CONTROL_TASK_END with error indication if possible, or rely on bot's timeout
        # This generic error might happen before original_requester_chat_id_str is known if args are wrong.
        # The print to stdout is the primary error communication to bot.py
        print(json.dumps({
            "status": "error", 
            "message": f"Forwarder script failed: {str(e)}", 
            "details": repr(e), 
            "count_sent_to_bot": count_forwarded_to_bot, # Could be partially complete
            "total_found": expected_count # Might be 0 if error was early
        }))
        return False
    finally:
        if client and client.is_connected():
            try:
                await client.disconnect()
                logger.info("Telethon client disconnected.")
            except Exception as e_disc:
                logger.error(f"Error during client disconnect: {e_disc}")


if __name__ == "__main__":
    if API_ID == 1234567 or API_HASH == "YOUR_API_HASH": 
        print(json.dumps({"status": "error", "message": "API_ID/HASH not set in forwarder.py", "count_sent_to_bot":0, "total_found":0}))
        sys.exit(1)
    
    if len(sys.argv) != 5:
        print(json.dumps({
            "status": "error", 
            "message": "Incorrect number of arguments for forwarder.py",
            "usage": "python forwarder.py <channel> <identifier> <bot_username> <orig_requester_id>",
            "count_sent_to_bot":0, "total_found":0
        }), file=sys.stdout) # Print to stdout for bot.py to catch
        sys.exit(1)

    pci_arg, mi_arg, tbu_arg, orci_arg = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    logger.info(f"Forwarder script started. Args: Source='{pci_arg}', Identifier='{mi_arg}', TargetBot='{tbu_arg}', OriginalUser='{orci_arg}'")
    
    success = False
    try:
        # Ensure client is defined globally for asyncio.run
        if 'client' not in globals() or client is None:
             logger.error("Telethon client was not initialized globally.")
             print(json.dumps({"status": "error", "message": "Telethon client not initialized.", "count_sent_to_bot":0, "total_found":0}))
             sys.exit(1)
        success = asyncio.run(main_logic(pci_arg, mi_arg, tbu_arg, orci_arg))
    except Exception as e_run:
        logger.error(f"Critical exception in forwarder __main__ execution: {e_run}", exc_info=True)
        # This print is crucial for bot.py to get an error status if main_logic crashes unexpectedly
        print(json.dumps({"status": "error", "message": f"forwarder.py main execution error: {str(e_run)}", "count_sent_to_bot":0, "total_found":0}))
    
    sys.exit(0 if success else 1)