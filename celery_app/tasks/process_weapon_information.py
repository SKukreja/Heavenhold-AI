import logging
import time
import json
import requests
import boto3
from celery import shared_task
from ..prompts.item_system_prompt import item_system
from ..prompts.weapon_prompt import weapon_prompt
from ..utils import make_api_call_with_backoff, format_option, format_engraving, redis_client, boto3_config
from .fetch_item_data import fetch_item_data
from config import DISCORD_CHANNEL_ID, WORDPRESS_SITE, AWS_S3_BUCKET, OPENAI_API_KEY



logger = logging.getLogger(__name__)

@shared_task(bind=True)
def process_weapon_information_task(self, key, folder, item_name):
    if key == "weapon-information/": return    
    global redis_client, AWS_S3_BUCKET, boto3_config, OPENAI_API_KEY
    attempt_count = int(redis_client.get('attempts:' + key) or 0)
    # Check the attempt count    
    if attempt_count >= 3:
        logger.info(f"Image {key} has reached maximum attempts. Deleting image.")
        s3_client.delete_object(Bucket=AWS_S3_BUCKET, Key=key)
        redis_client.delete('attempts:' + key)
        redis_client.delete('lock:' + key)
        return
    logger.info(f"Processing image: {key} from folder '{folder}' as weapon information (attempt {attempt_count + 1})")
    try:        
        # Retrieve cached data
        cached_data = redis_client.get('item_data')
        if cached_data is None:
            logger.warning("Item data not found in cache.")
            return
        
        item_data = json.loads(cached_data)
        item = next((i for i in item_data if i['slug'] == item_name), None)
        new_item = False
        if item is None:
            logger.warning(f"Item '{item_name}' not found, creating a new item.")
            new_item = True


        s3_client = boto3.client('s3', **boto3_config)

        # Generate a pre-signed URL for the image    
        pre_signed_url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': AWS_S3_BUCKET, 'Key': key},
            ExpiresIn=3600
        )

        # AI processing: Preparing the AI payload
        messages = [
            {
                "role": "system",
                "content": item_system,
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                    "text": weapon_prompt + json.dumps(item),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": pre_signed_url
                        },
                    }
                ],
            },
        ]
        
        payload = {
            "model": "gpt-4o",
            "messages": messages,
            "max_tokens": 1000,
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}"
        }

        # Make the API call to OpenAI
        response = make_api_call_with_backoff(
            "https://api.openai.com/v1/chat/completions",
            headers,
            payload
        )
        response.raise_for_status()
        response_json = response.json()

        # Process the AI response
        extracted_data = response_json['choices'][0]['message']['content']
        cleaned_data = extracted_data.strip('```json').strip('```')

        # Attempt to parse the extracted data as JSON
        try:
            item_info = json.loads(cleaned_data)
            logger.info("Successfully processed JSON from AI response")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON: {e}")
            return
        
        # Ensure 'main_option' is always a list
        main_option = item_info.get("main_option", [])
        if isinstance(main_option, dict):
            main_option = [main_option]
        item_info["main_option"] = main_option

        # Ensure 'sub_option' is always a list
        sub_option = item_info.get("sub_option", [])
        if isinstance(sub_option, dict):
            sub_option = [sub_option]
        item_info["sub_option"] = sub_option

        # Ensure 'engraving_option' is always a list
        engraving_options = item_info.get("engraving_options", [])
        if isinstance(engraving_options, dict):
            engraving_options = [engraving_options]
        item_info["engraving_options"] = engraving_options

        # Example payload for hero stats from AI response
        payload = {
            "name": item_info.get("name", 0),
            "rarity": item_info.get("rarity", 0),
            "weapon_type": item_info.get("weapon_type", 0),
            "exclusive": item_info.get("exclusive", 0),            
            "hero": item_info.get("hero", 0),
            "exclusive_effects": item_info.get("exclusive_effects", 0),
            "min_dps": item_info.get("min_dps", 0),
            "max_dps": item_info.get("max_dps", 0),
            "weapon_skill_name": item_info.get("weapon_skill_name", 0),
            "weapon_skill_atk": item_info.get("weapon_skill_atk", 0),
            "weapon_skill_regen_time": item_info.get("weapon_skill_regen_time", 0),
            "weapon_skill_description": item_info.get("weapon_skill_description", 0),
            "weapon_skill_chain": item_info.get("weapon_skill_chain", 0),
            "main_option": item_info["main_option"],
            "sub_option": item_info["sub_option"],
            "limit_break_5_option": item_info.get("limit_break_5_option", 0),
            "limit_break_5_value": item_info.get("limit_break_5_value", 0),
            "engraving_options": item_info["engraving_options"],
            "max_lines": item_info.get("max_lines", 0),
        }

        # Prepare and send the poll to Discord
        embed_data = {
            "title": f"Item Information - {item['title']}",
            "description": "Here's what I found in your image:",
            "color": 3447003,  # Blue
            "fields": [
                {"name": "Name", "value": str(payload["name"]), "inline": False} if payload["name"] != 0 else None,
                {"name": "Rarity", "value": str(payload["rarity"]), "inline": True} if payload["rarity"] != 0 else None,
                {"name": "Weapon Type", "value": str(payload["weapon_type"]), "inline": True} if payload["weapon_type"] != 0 else None,
                {"name": "Exclusive", "value": str(payload["exclusive"]), "inline": True} if payload["exclusive"] != 0 else None,
                {"name": "Hero", "value": str(payload["hero"]), "inline": True} if payload["hero"] != 0 else None,
                {"name": "Exclusive Effects", "value": str(payload["exclusive_effects"]), "inline": False} if payload["exclusive_effects"] != 0 else None,
                {"name": "Min DPS", "value": str(payload["min_dps"]), "inline": True} if payload["min_dps"] != 0 else None,
                {"name": "Max DPS", "value": str(payload["max_dps"]), "inline": True} if payload["max_dps"] != 0 else None,
                {"name": "Weapon Skill Name", "value": str(payload["weapon_skill_name"]), "inline": False} if payload["weapon_skill_name"] != 0 else None,
                {"name": "Weapon Skill Atk", "value": str(payload["weapon_skill_atk"]), "inline": True} if payload["weapon_skill_atk"] != 0 else None,
                {"name": "Weapon Skill Regen Time", "value": str(payload["weapon_skill_regen_time"]), "inline": True} if payload["weapon_skill_regen_time"] != 0 else None,
                {"name": "Weapon Skill Chain", "value": str(payload["weapon_skill_chain"]), "inline": True} if payload["weapon_skill_chain"] != 0 else None, 
                {"name": "Weapon Skill Description", "value": str(payload["weapon_skill_description"]), "inline": False} if payload["weapon_skill_description"] != 0 else None,               
                {"name": "Main Option", "value": "\n".join(format_option(opt) for opt in payload["main_option"]) if payload["main_option"] is not None else "", "inline": False},
                {"name": "Sub Option", "value": "\n".join(format_option(opt) for opt in payload["sub_option"]) if payload["sub_option"] is not None else "", "inline": False},
                {"name": "Engraving Options", "value": "\n".join(format_engraving(opt) for opt in payload["engraving_options"]) if payload["engraving_options"] is not None else "", "inline": False},
                {"name": "Limit Break 5 Option", "value": str(payload["limit_break_5_option"]), "inline": True} if payload["limit_break_5_option"] != 0 else None,
                {"name": "Limit Break 5 Value", "value": str(payload["limit_break_5_value"]), "inline": True} if payload["limit_break_5_value"] != 0 else None,
                {"name": "Max Lines", "value": str(payload["max_lines"]), "inline": True} if payload["max_lines"] != 0 else None,
            ],
            "footer": {"text": "Does this look correct?"}
        }

        # Remove any None fields (in case some stats are not present)
        embed_data["fields"] = [field for field in embed_data["fields"] if field]

        # Send poll request to Discord through Redis
        poll_data = {
            'channel_id': DISCORD_CHANNEL_ID, 
            'is_embed': True,
            'embed': embed_data,
            'task_id': process_weapon_information_task.request.id
        }
        redis_client.rpush('discord_message_queue', json.dumps(poll_data))
        logger.info(f"Sent poll to Discord for item: {item['title']}")

        # Wait for poll result (e.g., 60 seconds)
        result_key = f"discord_poll_result:{process_weapon_information_task.request.id}"
        upvotes, downvotes, retry_count = 0, 0, 0

        for _ in range(100):  # Check every second, up to 120 seconds
            poll_result = redis_client.get(result_key)
            if poll_result:
                poll_result_data = json.loads(poll_result)
                upvotes = poll_result_data.get('upvotes', 0)
                downvotes = poll_result_data.get('downvotes', 0)
                retry_count = poll_result_data.get('retry', 0)
                redis_client.delete(result_key)
                break
            time.sleep(1)

        logger.info("Checking poll results: Upvotes - %d, Downvotes - %d", upvotes, downvotes)
        
        update_url = f"{WORDPRESS_SITE}/wp-json/heavenhold/v1/update-weapon"
        
        # If upvotes are higher than downvotes, post the data to WordPress
        if retry_count > 0:
            logger.info(f"Retrying processing for weapon {item['title']}")
            # Reset attempt count
            redis_client.set('attempts:' + key, 0)
            redis_client.delete('lock:' + key)
            return
        if upvotes > downvotes:
            response = requests.post(update_url, json={
                "item_id": item['databaseId'] if not new_item else 0,
                "name": payload.get("name", 0),
                "rarity": payload.get("rarity", 0),
                "weapon_type": payload.get("weapon_type", 0),
                "exclusive": payload.get("exclusive", 0),          
                "hero": payload.get("hero", 0),
                "exclusive_effects": payload.get("exclusive_effects", 0),
                "min_dps": payload.get("min_dps", 0),
                "max_dps": payload.get("max_dps", 0),
                "weapon_skill_name": payload.get("weapon_skill_name", 0),
                "weapon_skill_atk": payload.get("weapon_skill_atk", 0),
                "weapon_skill_regen_time": payload.get("weapon_skill_regen_time", 0),
                "weapon_skill_description": payload.get("weapon_skill_description", 0),
                "weapon_skill_chain": payload.get("weapon_skill_chain", 0),
                "main_option": payload.get("main_option", []),
                "sub_option": payload.get("sub_option", []),
                "limit_break_5_option": payload.get("limit_break_5_option", 0),
                "limit_break_5_value": payload.get("limit_break_5_value", 0),
                "engraving_options": payload.get("engraving_options", []),
                "max_lines": payload.get("max_lines", 0),       
                'confirmed': True
            })
            response.raise_for_status()
            logger.info(f"Item information updated successfully for weapon {item['title']}")
        elif upvotes == 0 and downvotes == 0:
            response = requests.post(update_url, json={
                "item_id": item['databaseId'],
                "name": payload.get("name", 0),
                "rarity": payload.get("rarity", 0),
                "weapon_type": payload.get("weapon_type", 0),
                "exclusive": payload.get("exclusive", 0),          
                "hero": payload.get("hero", 0),
                "exclusive_effects": payload.get("exclusive_effects", 0),
                "min_dps": payload.get("min_dps", 0),
                "max_dps": payload.get("max_dps", 0),
                "weapon_skill_name": payload.get("weapon_skill_name", 0),
                "weapon_skill_atk": payload.get("weapon_skill_atk", 0),
                "weapon_skill_regen_time": payload.get("weapon_skill_regen_time", 0),
                "weapon_skill_description": payload.get("weapon_skill_description", 0),
                "weapon_skill_chain": payload.get("weapon_skill_chain", 0),
                "main_option": payload.get("main_option", []),
                "sub_option": payload.get("sub_option", []),
                "limit_break_5_option": payload.get("limit_break_5_option", 0),
                "limit_break_5_value": payload.get("limit_break_5_value", 0),
                "engraving_options": payload.get("engraving_options", []),
                "max_lines": payload.get("max_lines", 0),          
                'confirmed': False
            })
            response.raise_for_status()
            logger.info(f"Item information revision created successfully for weapon {item['title']}")
        else:
            logger.info(f"Aborting update for weapon {item['title']}")
        # Delete the image after processing (if desired)
        s3_client.delete_object(Bucket=AWS_S3_BUCKET, Key=key)
        redis_client.delete('attempts:' + key)
        redis_client.delete('lock:' + key)
        logger.info(f"{key} processed successfully, deleting from S3 bucket.")
        fetch_item_data.delay()
    except Exception as e:
        # Increment the attempt count
        attempt_count = redis_client.incr('attempts:' + key)
        if attempt_count >= 3:
            logger.exception(f"Error processing image {key}. Max attempts reached. Deleting image.")
            s3_client.delete_object(Bucket=AWS_S3_BUCKET, Key=key)
            redis_client.delete('attempts:' + key)
            redis_client.delete('lock:' + key)
        else:
            logger.exception(f"Error processing image {key}. Retrying after 180 seconds.")
            redis_client.delete('lock:' + key)
            raise self.retry(exc=e, countdown=180)