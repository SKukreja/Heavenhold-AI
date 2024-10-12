# app/celery_tasks.py

import logging
import time
import boto3
import os
import base64
import io
import tempfile
from flask import current_app
from .app import celery, bucket_name, boto3_config, api_key
from .stat_prompt import stat_prompt
from .assistant_prompt import system_prompt
from .weapon_prompt import weapon_prompt
from .hero_query import hero_query
from .item_query import item_query
import requests
import json
import redis
from celery import Task
import numpy as np
import threading
from PIL import Image

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s: %(levelname)s/%(processName)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

redis_client = redis.Redis(host='redis-service', port=6379, db=0)

def make_api_call_with_backoff(url, headers, payload, max_retries=10, backoff_factor=1, max_delay=600):
    delay = backoff_factor
    last_exception = None
    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload)
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as e:
            last_exception = e
            response = e.response
            if response.status_code == 429:
                # Rate limit exceeded
                # Get the 'Retry-After' header if present
                retry_after = response.headers.get('Retry-After')
                if retry_after:
                    delay = int(retry_after)
                else:
                    delay = min(delay * 8, max_delay)  # Exponential backoff
                logger.warning(f"Rate limit exceeded. Retrying in {delay} seconds.")
                time.sleep(delay)
            else:
                # Other HTTP errors
                logger.error(f"HTTP error occurred: {e}")
                logger.error(f"Response content: {response.text}")
                raise
        except requests.exceptions.RequestException as e:
            # Network errors
            logger.error(f"Network error occurred: {e}")
            raise
    logger.error("Max retries exceeded")
    raise last_exception if last_exception else Exception("Max retries exceeded")

# Set up periodic tasks
@celery.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs):
    # Schedule to check 'hero-stories' folder every 60 seconds with staggered delays
    sender.add_periodic_task(
        30.0,  # Run every 60 seconds
        check_and_process_s3_images.s('hero-stories'),
        name="Check 'hero-stories' folder in S3 bucket",
        countdown=0,  # No delay
    )

    sender.add_periodic_task(
        30.0,  # Run every 60 seconds
        check_and_process_s3_images.s('hero-portraits'),
        name="Check 'hero-portraits' folder in S3 bucket",
        countdown=10,  # Stagger by 10 seconds
    )

    sender.add_periodic_task(
        30.0,  # Run every 60 seconds
        check_and_process_s3_images.s('hero-illustrations'),
        name="Check 'hero-illustrations' folder in S3 bucket",
        countdown=20,  # Stagger by 20 seconds
    )

    sender.add_periodic_task(
        30.0,  # Run every 60 seconds
        check_and_process_s3_images.s('hero-bios'),
        name="Check 'hero-bios' folder in S3 bucket",
        countdown=30,  # Stagger by 30 seconds
    )

    sender.add_periodic_task(
        30.0,  # Run every 60 seconds
        check_and_process_s3_images.s('hero-stats'),
        name="Check 'hero-stats' folder in S3 bucket",
        countdown=40,  # Stagger by 40 seconds
    )

    # Schedule to fetch hero stories data every 3 minutes
    sender.add_periodic_task(
        180.0,  # Run every 3 minutes
        fetch_hero_data.s(),
        name="Fetch hero data from WordPress",
        countdown=0  # No delay needed
    )

    # Schedule to fetch hero stories data every 3 minutes
    sender.add_periodic_task(
        180.0,  # Run every 3 minutes
        fetch_item_data.s(),
        name="Fetch item data from WordPress",
        countdown=10  # No delay needed
    )

    # Trigger tasks immediately on startup
    fetch_hero_data.delay()
    fetch_item_data.delay()


@celery.task
def fetch_hero_data():
    logger.info("Fetching hero data from WordPress")
    try:
        # Prepare the GraphQL query
        query = hero_query

        # Prepare the request
        url = current_app.config['WORDPRESS_SITE'] + '/graphql'
        auth = (
            current_app.config['WORDPRESS_USERNAME'],
            current_app.config['WORDPRESS_PASSWORD'],
        )
        headers = {'Content-Type': 'application/json'}
        data = {'query': query}

        # Make the request
        response = requests.post(url, json=data, headers=headers)
        response.raise_for_status()

        # Parse the response
        result = response.json()

        # Store the result in Redis
        redis_client.set('hero_data', json.dumps(result))

        logger.info("Hero data cached successfully")

        # Log a summary of the data
        hero_count = len(result['data']['heroes']['nodes'])
        logger.info(f"Cached data contains {hero_count} heroes.")
    except Exception as e:
        logger.exception("Error fetching hero data:")

@celery.task
def fetch_item_data():
    logger.info("Fetching item data from WordPress")
    try:
        # Prepare the GraphQL query
        query = item_query

        # Prepare the request
        url = current_app.config['WORDPRESS_SITE'] + '/graphql'
        auth = (
            current_app.config['WORDPRESS_USERNAME'],
            current_app.config['WORDPRESS_PASSWORD'],
        )
        headers = {'Content-Type': 'application/json'}
        data = {'query': query}

        # Make the request
        response = requests.post(url, json=data, headers=headers)
        response.raise_for_status()

        # Parse the response
        result = response.json()

        # Store the result in Redis
        redis_client.set('item_data', json.dumps(result))

        logger.info("Item data cached successfully")

        # Log a summary of the data
        item_count = len(result['data']['items']['nodes'])
        logger.info(f"Cached data contains {item_count} items.")
    except Exception as e:
        logger.exception("Error fetching item data:")

@celery.task(bind=True)
def process_hero_story_task(self, key, folder, hero_name):
    if key == "hero-stories/": return
    global redis_client, bucket_name, boto3_config, api_key
    attempt_count = int(redis_client.get('attempts:' + key) or 0)
    # Check the attempt count    
    if attempt_count >= 3:
        logger.info(f"Image {key} has reached maximum attempts. Deleting image.")
        s3_client.delete_object(Bucket=bucket_name, Key=key)
        redis_client.delete('attempts:' + key)
        redis_client.delete('lock:' + key)
        return
    logger.info(f"Processing image: {key} from folder '{folder}' as a hero story (attempt {attempt_count + 1})")     
    try:
        # Retrieve cached data
        cached_data = redis_client.get('hero_data')
        if cached_data is None:
            logger.warning("Hero data not found in cache.")
            return
        
        hero_data = json.loads(cached_data)
        hero = next((h for h in hero_data['data']['heroes']['nodes'] if h['slug'] == hero_name), None)

        if hero is None:
            logger.warning(f"Hero '{hero_name}' not found.")
            return
        
        s3_client = boto3.client('s3', **boto3_config)

        # Generate a pre-signed URL for the image       
        pre_signed_url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket_name, 'Key': key},
            ExpiresIn=3600  
        )

        # Prepare the messages
        messages = [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Please analyze this image and generate a JSON object containing only the updated story for this hero. The hero's story should be recorded exactly as written, but you will only receive part of it on each screenshot. Check the current data for this hero to see if part of the story has already been recorded, and then append or prepend the new parts you see in the image, piecing together as much of the full story as you can in your output. If the current story already has more than what you see, do not change it. Respond with only valid JSON data to import. Current data for this hero: " + json.dumps(hero['heroInformation']['bioFields']),
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

        # Prepare the data payload (as JSON)
        payload = {
            "model": "gpt-4o",
            "messages": messages,
            "max_tokens": 1000,
        }

        # Set up headers
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }

        # Make the API call
        response = make_api_call_with_backoff(
            "https://api.openai.com/v1/chat/completions",
            headers,
            payload
        )

        # Parse the JSON content
        response.raise_for_status()
        response_json = response.json()            

        # Access the 'choices' data
        extracted_data = response_json['choices'][0]['message']['content']
        cleaned_data = extracted_data.strip('```json').strip('```')

        # Attempt to parse the extracted data as JSON
        try:                
            hero_story = json.loads(cleaned_data)
            logger.info("Successfully processed JSON from AI response")

            payload = {
                'hero_id': hero.get('databaseId', 0),
                'story': hero_story.get('story', 0),
            }

            # Prepare and send the poll to Discord
            embed_data = {
                "title": f"Hero Story - {hero['title']}",
                "description": "Here's what I found in your image:\n\n" + payload['story'],
                "color": 3447003,                
                "footer": {"text": "Does this look correct?"}
            }

            # Remove any None fields (in case some stats are not present)
            embed_data["fields"] = [field for field in embed_data["fields"] if field]
            
            # Send poll request to Discord through Redis
            poll_data = {
                'channel_id': current_app.config['DISCORD_CHANNEL_ID'], 
                'is_embed': True,
                'embed': embed_data,
                'task_id': process_hero_story_task.request.id
            }
            redis_client.rpush('discord_message_queue', json.dumps(poll_data))
            logger.info(f"Sent poll to Discord for hero: {hero['title']}")
            
            # Wait for poll result (e.g., 60 seconds)
            result_key = f"discord_poll_result:{process_hero_story_task.request.id}"
            upvotes, downvotes = 0, 0

            for _ in range(120):  # Check every second, up to 120 seconds
                poll_result = redis_client.get(result_key)
                if poll_result:
                    poll_result_data = json.loads(poll_result)
                    upvotes = poll_result_data.get('upvotes', 0)
                    downvotes = poll_result_data.get('downvotes', 0)
                    redis_client.delete(result_key)
                    break
                time.sleep(1)

            logger.info("Checking poll results: Upvotes - %d, Downvotes - %d", upvotes, downvotes)

            update_url = current_app.config['WORDPRESS_SITE'] + '/wp-json/heavenhold/v1/update-story'

            # If upvotes are higher than downvotes, post the data to WordPress
            if upvotes > downvotes:
                response = requests.post(update_url, json={
                    'hero_id': hero['databaseId'],
                    'story': payload['story'],
                    'confirmed': True
                })
                response.raise_for_status()
                logger.info("Hero story updated successfully")
            elif upvotes == 0 and downvotes == 0:
                response = requests.post(update_url, json={
                    'hero_id': hero['databaseId'],
                    'story': payload['story'],
                    'confirmed': True
                })
                response.raise_for_status()
                logger.info("Hero story updated successfully")
            else:
                logger.info(f"Aborting story update for {hero['title']}")

            # Delete the image after processing (if desired)
            s3_client.delete_object(Bucket=bucket_name, Key=key)
            redis_client.delete('attempts:' + key)
            redis_client.delete('lock:' + key)
            logger.info(f"{key} processed successfully, deleting from S3 bucket.")
        except json.JSONDecodeError as e:
            logger.error("Failed to parse JSON from AI response")
            logger.error(e)
    except Exception as e:
        # Increment the attempt count
        attempt_count = redis_client.incr('attempts:' + key)
        if attempt_count >= 3:
            logger.exception(f"Error processing image {key}. Max attempts reached. Deleting image.")
            s3_client.delete_object(Bucket=bucket_name, Key=key)
            redis_client.delete('attempts:' + key)
            redis_client.delete('lock:' + key)
        else:
            logger.exception(f"Error processing image {key}. Retrying after 180 seconds.")
            redis_client.delete('lock:' + key)
            raise self.retry(exc=e, countdown=180)

@celery.task(bind=True)
def process_hero_portrait_task(self, key, folder, hero_name, region):
    if key == "hero-portraits/": return
    global redis_client, bucket_name, boto3_config, api_key
    attempt_count = int(redis_client.get('attempts:' + key) or 0)
    # Check the attempt count    
    if attempt_count >= 3:
        logger.info(f"Image {key} has reached maximum attempts. Deleting image.")
        s3_client.delete_object(Bucket=bucket_name, Key=key)
        redis_client.delete('attempts:' + key)
        redis_client.delete('lock:' + key)
        return
    logger.info(f"Processing image: {key} from folder '{folder}' as a hero portrait (attempt {attempt_count + 1})")    
    try:
        # Retrieve cached data
        cached_data = redis_client.get('hero_data')
        if cached_data is None:
            logger.warning("Hero data not found in cache.")
            return
        
        hero_data = json.loads(cached_data)
        hero = next((h for h in hero_data['data']['heroes']['nodes'] if h['slug'] == hero_name), None)

        if hero is None:
            logger.warning(f"Hero '{hero_name}' not found.")
            return

        # Initialize S3 client using app.config variables
        s3_client = boto3.client('s3', **boto3_config)

        # Retrieve and process the image from S3
        response = s3_client.get_object(
            Bucket=bucket_name,
            Key=key
        )
        image_content = response['Body'].read()

        # Create a temporary directory
        with tempfile.TemporaryDirectory() as temp_dir:
            # Save the image in the temporary directory
            image_path = os.path.join(temp_dir, "image.jpg")
            with open(image_path, 'wb') as f:
                f.write(image_content)

            # Detect the black bar width
            left_bar, right_bar = detect_black_bar_width(image_path)
            print(f"Detected black bar width: Left - {left_bar}px, Right - {right_bar}px")

            # After determining the bar widths, you can crop the image accordingly:
            img = Image.open(image_path)
            cropped_img = img.crop((left_bar, 0, img.width - right_bar, img.height))

            # Rotate the image if cropped width is longer than height
            if cropped_img.width > cropped_img.height:
                cropped_img = cropped_img.rotate(-90, expand=True)

            # Save the cropped image to separate BytesIO objects
            img_byte_arr_base64 = io.BytesIO()
            cropped_img.save(img_byte_arr_base64, format='JPEG')
            image_bytes = img_byte_arr_base64.getvalue()

            img_byte_arr = io.BytesIO()
            cropped_img.save(img_byte_arr, format='JPEG')
            img_byte_arr.seek(0)

            # Prepare and send the poll to Discord
            embed_data = {
                "title": f"Hero Portrait - {hero['title']}",
                "description": "I did my best!",
                "color": 3447003,  # Example blue color
                "fields": [
                    {"name": "Region", "value": region, "inline": True} if region else None,                        
                ],
                "footer": {"text": "Does this look correct?"}
            }

            # Remove any None fields (in case some stats are not present)
            embed_data["fields"] = [field for field in embed_data["fields"] if field]

            # Convert the image to Base64
            base64_image = encode_image_to_base64(image_bytes)
            
            # Send poll request to Discord through Redis
            poll_data = {
                'channel_id': current_app.config['DISCORD_CHANNEL_ID'], 
                'is_embed': True,
                'embed': embed_data,
                'image': base64_image,
                'filename': hero_name + '.jpg',
                'task_id': process_hero_portrait_task.request.id
            }
            redis_client.rpush('discord_message_queue', json.dumps(poll_data))
            logger.info(f"Sent poll to Discord for hero: {hero['title']}")
            
            # Wait for poll result (e.g., 120 seconds)
            result_key = f"discord_poll_result:{process_hero_portrait_task.request.id}"
            upvotes, downvotes = 0, 0

            for _ in range(120):
                poll_result = redis_client.get(result_key)
                if poll_result:
                    poll_result_data = json.loads(poll_result)
                    upvotes = poll_result_data.get('upvotes', 0)
                    downvotes = poll_result_data.get('downvotes', 0)
                    redis_client.delete(result_key)
                    break
                time.sleep(1)

            logger.info("Checking poll results: Upvotes - %d, Downvotes - %d", upvotes, downvotes)

            # Prepare the files and payload
            img_byte_arr.seek(0)
            files = {
                'image': (hero_name + '.jpg', img_byte_arr, 'image/jpeg')
            }
            payload = {
                'hero_id': str(hero['databaseId']),
                'region': str(region),
                'confirmed': '1' if upvotes > downvotes else '0'
            }

            # Log the data being sent
            logger.info(f"Sending data: {payload}")
            logger.info(f"Sending files: {files}")

            # Send the POST request
            try:
                update_url = current_app.config['WORDPRESS_SITE'] + '/wp-json/heavenhold/v1/update-portrait'
                response = requests.post(update_url, files=files, data=payload)
                response.raise_for_status()
                logger.info("Hero portrait updated successfully")
            except requests.exceptions.HTTPError as e:
                logger.error(f"HTTP error occurred: {e}")
                logger.error(f"Response content: {response.text}")
                raise

            # Delete the image after processing
            s3_client.delete_object(Bucket=bucket_name, Key=key)
            redis_client.delete('attempts:' + key)
            redis_client.delete('lock:' + key)
            logger.info(f"{key} processed successfully, deleting from S3 bucket.")
    except Exception as e:
        # Increment the attempt count
        attempt_count = redis_client.incr('attempts:' + key)
        if attempt_count >= 3:
            logger.exception(f"Error processing image {key}. Max attempts reached. Deleting image.")
            s3_client.delete_object(Bucket=bucket_name, Key=key)
            redis_client.delete('attempts:' + key)
            redis_client.delete('lock:' + key)
        else:
            logger.exception(f"Error processing image {key}. Retrying after 180 seconds.")
            redis_client.delete('lock:' + key)
            raise self.retry(exc=e, countdown=180)

@celery.task(bind=True)
def process_hero_illustration_task(self, key, folder, hero_name, region):
    if key == "hero-illustrations/": return
    global redis_client, bucket_name, boto3_config, api_key
    attempt_count = int(redis_client.get('attempts:' + key) or 0)
    # Check the attempt count    
    if attempt_count >= 3:
        logger.info(f"Image {key} has reached maximum attempts. Deleting image.")
        s3_client.delete_object(Bucket=bucket_name, Key=key)
        redis_client.delete('attempts:' + key)
        redis_client.delete('lock:' + key)
        return
    logger.info(f"Processing image: {key} from folder '{folder}' as a hero illustration and thumbnail (attempt {attempt_count + 1})")   
    try:
        # Retrieve cached data
        cached_data = redis_client.get('hero_data')
        if cached_data is None:
            logger.warning("Hero data not found in cache.")
            return
        
        hero_data = json.loads(cached_data)
        hero = next((h for h in hero_data['data']['heroes']['nodes'] if h['slug'] == hero_name), None)

        if hero is None:
            logger.warning(f"Hero '{hero_name}' not found.")
            return

        # Initialize S3 client using app.config variables
        s3_client = boto3.client('s3', **boto3_config)

        # Generate a pre-signed URL for the image
        
        pre_signed_url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket_name, 'Key': key},
            ExpiresIn=3600  
        )

        # Retrieve and process the image from S3
        s3_response = s3_client.get_object(
            Bucket=bucket_name,
            Key=key
        )
        image_content = s3_response['Body'].read()

        # Save image to a temporary location
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = os.path.join(temp_dir, "image.png")
            with open(image_path, 'wb') as f:
                f.write(image_content)
            original_img = Image.open(image_path)

            # Prepare the messages
            messages = [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Please analyze this image and generate a JSON object containing the top left pixel coordinates of a crop box that crops the face of the character you see in the image. Respond with only valid JSON data to import. The JSON keys to include are 'x', 'y', 'width', and 'height'. The thumbnail crop should be a 1:1 square image framing the character's entire face. Ensure the crop coordinates are proportional to the resolution of the image. Calculate x, y, width, and height values such that their resulting crop box would not exceed 500x500px and covers the face of the character in the image's original resolution, which is " + str(original_img.size[0]) + "x" + str(original_img.size[1]) + " pixels. Determine the resolution of the image you're looking at, and multiply the values by how much the resolution has been downscaled in the version of the image you're looking at.", 
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

            # Prepare the data payload (as JSON)
            payload = {
                "model": "gpt-4o",
                "messages": messages,
                "max_tokens": 1000,
            }

            # Set up headers
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }

            # Make the API call
            response = make_api_call_with_backoff(
                "https://api.openai.com/v1/chat/completions",
                headers,
                payload
            )

            # Parse the JSON content
            response.raise_for_status()
            response_json = response.json()            

            # Access the 'choices' data
            extracted_data = response_json['choices'][0]['message']['content']
            cleaned_data = extracted_data.strip('```json').strip('```')

            # Log the response JSON
            logger.info(cleaned_data)
            
            # Save the image to separate BytesIO objects
            img_byte_arr_base64 = io.BytesIO()
            original_img.save(img_byte_arr_base64, format='PNG')
            image_bytes = img_byte_arr_base64.getvalue()

            img_byte_arr = io.BytesIO()
            original_img.save(img_byte_arr, format='PNG')
            img_byte_arr.seek(0)
            
            # Attempt to parse the extracted data as JSON
            try:  
                crop_data = json.loads(cleaned_data)

                # Prepare and send the poll to Discord
                embed_data = {
                    "title": f"Hero Illustration - {hero['title']}",
                    "description": "Here's what you gave me:",
                    "color": 3447003,  # Example blue color
                    "fields": [
                        {"name": "Region", "value": region, "inline": True} if region else None,
                        {"name": "Crop Data", "value": f"x: {crop_data['x']}, y: {crop_data['y']}, width: {crop_data['width']}, height: {crop_data['height']}", "inline": False},
                    ],
                    "footer": {"text": "Does this look correct?"}
                }

                # Remove any None fields
                embed_data["fields"] = [field for field in embed_data["fields"] if field]

                # Convert the image to Base64
                base64_image = encode_image_to_base64(image_bytes)

                # Send poll request to Discord through Redis
                poll_data = {
                    'channel_id': current_app.config['DISCORD_CHANNEL_ID'], 
                    'is_embed': True,
                    'embed': embed_data,
                    'image': base64_image,
                    'filename': hero_name + '.png',
                    'task_id': process_hero_illustration_task.request.id
                }
                redis_client.rpush('discord_message_queue', json.dumps(poll_data))
                logger.info(f"Sent poll to Discord for hero: {hero['title']}")
                
                # Wait for poll result
                result_key = f"discord_poll_result:{process_hero_illustration_task.request.id}"
                upvotes, downvotes = 0, 0

                for _ in range(120):
                    poll_result = redis_client.get(result_key)
                    if poll_result:
                        poll_result_data = json.loads(poll_result)
                        upvotes = poll_result_data.get('upvotes', 0)
                        downvotes = poll_result_data.get('downvotes', 0)
                        redis_client.delete(result_key)
                        break
                    time.sleep(1)

                logger.info("Checking poll results: Upvotes - %d, Downvotes - %d", upvotes, downvotes)
                
                # Prepare the files and payload
                img_byte_arr.seek(0)
                files = {
                    'image': (hero_name + '.png', img_byte_arr, 'image/png')
                }
                payload = {
                    'hero_id': str(hero['databaseId']),
                    'region': str(region),
                    'x': str(crop_data.get('x', 0)),
                    'y': str(crop_data.get('y', 0)),
                    'width': str(crop_data.get('width', 0)),
                    'height': str(crop_data.get('height', 0)),
                    'confirmed': '1' if upvotes > downvotes else '0'
                }

                # Log the data being sent
                logger.info(f"Sending data: {payload}")
                logger.info(f"Sending files: {files}")

                # Send the POST request with form-data
                try:
                    update_url = current_app.config['WORDPRESS_SITE'] + '/wp-json/heavenhold/v1/update-illustration'
                    response = requests.post(update_url, files=files, data=payload)
                    response.raise_for_status()
                    logger.info("Hero illustration/thumbnail updated successfully")
                except requests.exceptions.HTTPError as e:
                    logger.error(f"HTTP error occurred: {e}")
                    logger.error(f"Response content: {response.text}")
                    raise
                
                # Delete the image after processing
                s3_client.delete_object(Bucket=bucket_name, Key=key)
                redis_client.delete('attempts:' + key)
                redis_client.delete('lock:' + key)
                logger.info(f"{key} processed successfully, deleting from S3 bucket.")
            except json.JSONDecodeError as e:
                logger.error("Failed to parse JSON from AI response")
                logger.error(e)
    except Exception as e:
        # Increment the attempt count
        attempt_count = redis_client.incr('attempts:' + key)
        if attempt_count >= 3:
            logger.exception(f"Error processing image {key}. Max attempts reached. Deleting image.")
            s3_client.delete_object(Bucket=bucket_name, Key=key)
            redis_client.delete('attempts:' + key)
            redis_client.delete('lock:' + key)
        else:
            logger.exception(f"Error processing image {key}. Retrying after 180 seconds.")
            redis_client.delete('lock:' + key)
            raise self.retry(exc=e, countdown=180)


@celery.task(bind=True)
def process_hero_bio_task(self, key, folder, hero_name):
    if key == "hero-bios/": return
    global redis_client, bucket_name, boto3_config, api_key
    attempt_count = int(redis_client.get('attempts:' + key) or 0)
    # Check the attempt count    
    if attempt_count >= 3:
        logger.info(f"Image {key} has reached maximum attempts. Deleting image.")
        s3_client.delete_object(Bucket=bucket_name, Key=key)
        redis_client.delete('attempts:' + key)
        redis_client.delete('lock:' + key)
        return
    logger.info(f"Processing image: {key} from folder '{folder}' as hero bio information (attempt {attempt_count + 1})")
    try:
        # Retrieve cached data
        cached_data = redis_client.get('hero_data')
        if cached_data is None:
            logger.warning("Hero data not found in cache.")
            return
        
        hero_data = json.loads(cached_data)
        hero = next((h for h in hero_data['data']['heroes']['nodes'] if h['slug'] == hero_name), None)

        if hero is None:
            logger.warning(f"Hero '{hero_name}' not found.")
            return

        # Initialize S3 client using app.config variables
        s3_client = boto3.client('s3', **boto3_config)

        # Generate a pre-signed URL for the image
        pre_signed_url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket_name, 'Key': key},
            ExpiresIn=3600  
        )

        # Prepare the messages
        messages = [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Please analyze this image and generate a JSON object containing values for 'height', 'weight', 'age', 'species', 'role', and 'element' from the 'Hero Information' section in the screenshot. Respond with only valid JSON using the mentioned keys, and ignore any icons or other irrelevant information. If the existing data for a particular key is more complete than what you find, use the pre-existing value in your JSON response. Current data for this hero: " + json.dumps(hero['heroInformation']['bioFields']), 
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

        # Prepare the data payload (as JSON)
        payload = {
            "model": "gpt-4o",
            "messages": messages,
            "max_tokens": 1000,
        }

        # Set up headers
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }

        # Make the API call
        response = make_api_call_with_backoff(
            "https://api.openai.com/v1/chat/completions",
            headers,
            payload
        )

        # Parse the JSON content
        response.raise_for_status()
        response_json = response.json()            

        # Access the 'choices' data
        extracted_data = response_json['choices'][0]['message']['content']
        cleaned_data = extracted_data.strip('```json').strip('```')

        # Attempt to parse the extracted data as JSON
        try:                
            hero_bio = json.loads(cleaned_data)
            logger.info("Successfully processed JSON from AI response")
            
            payload = {
                'hero_id': hero.get('databaseId', 0),
                'age': hero_bio.get('age', 0),
                'height': hero_bio.get('height', 0),
                'weight': hero_bio.get('weight', 0),
                'species': hero_bio.get('species', 0),
                'role': hero_bio.get('role', 0),
                'element': hero_bio.get('element', 0),
            }

            # Prepare and send the poll to Discord
            embed_data = {
                "title": f"Hero Bio - {hero['title']}",
                "description": "Here's what I found in your image:",
                "color": 3447003,  # Example blue color
                "fields": [
                    {"name": "Age", "value": payload["age"], "inline": True} if payload["age"] != 0 else None,
                    {"name": "Height", "value": payload["height"], "inline": True} if payload["height"] != 0 else None,
                    {"name": "Weight", "value": payload["weight"], "inline": True} if payload["weight"] != 0 else None,
                    {"name": "Species", "value": payload["species"], "inline": True} if payload["species"] != 0 else None,
                    {"name": "Role", "value": payload["role"], "inline": True} if payload["role"] != 0 else None,
                    {"name": "Element", "value": payload["element"], "inline": True} if payload["element"] != 0 else None,
                ],
                "footer": {"text": "Does this look correct?"}
            }

            # Remove any None fields (in case some stats are not present)
            embed_data["fields"] = [field for field in embed_data["fields"] if field]
            
            # Send poll request to Discord through Redis
            poll_data = {
                'channel_id': current_app.config['DISCORD_CHANNEL_ID'], 
                'is_embed': True,
                'embed': embed_data,
                'task_id': process_hero_bio_task.request.id
            }
            redis_client.rpush('discord_message_queue', json.dumps(poll_data))
            logger.info(f"Sent poll to Discord for hero: {hero['title']}")
            
            # Wait for poll result (e.g., 60 seconds)
            result_key = f"discord_poll_result:{process_hero_bio_task.request.id}"
            upvotes, downvotes = 0, 0

            for _ in range(120):  # Check every second, up to 120 seconds
                poll_result = redis_client.get(result_key)
                if poll_result:
                    poll_result_data = json.loads(poll_result)
                    upvotes = poll_result_data.get('upvotes', 0)
                    downvotes = poll_result_data.get('downvotes', 0)
                    redis_client.delete(result_key)
                    break
                time.sleep(1)

            logger.info("Checking poll results: Upvotes - %d, Downvotes - %d", upvotes, downvotes)

            update_url = current_app.config['WORDPRESS_SITE'] + '/wp-json/heavenhold/v1/update-bio'

            # If upvotes are higher than downvotes, post the data to WordPress
            if upvotes > downvotes:
                response = requests.post(update_url, json={
                    'hero_id': hero['databaseId'],
                    'age':payload['age'],
                    'height': payload['height'],
                    'weight': payload['weight'],
                    'species': payload['species'],
                    'role': payload['role'],
                    'element': payload['element'],
                    'confirmed': True
                })
                response.raise_for_status()
                logger.info(f"Hero bio updated successfully for hero {hero['title']}")
            elif upvotes == 0 and downvotes == 0:
                response = requests.post(update_url, json={
                    'hero_id': hero['databaseId'],
                    'age':payload['age'],
                    'height': payload['height'],
                    'weight': payload['weight'],
                    'species': payload['species'],
                    'role': payload['role'],
                    'element': payload['element'],
                    'confirmed': False
                })
                response.raise_for_status()
                logger.info(f"Hero bio updated successfully for hero {hero['title']}")
            else:
                logger.info(f"Aborting bio update for {hero['title']}")
            # Delete the image after processing (if desired)
            s3_client.delete_object(Bucket=bucket_name, Key=key)     
            redis_client.delete('attempts:' + key)          
            redis_client.delete('lock:' + key)
            logger.info(f"{key} processed successfully, deleting from S3 bucket.")
        except json.JSONDecodeError as e:
            logger.error("Failed to parse JSON from AI response")
            logger.error(e)
    except Exception as e:
        # Increment the attempt count
        attempt_count = redis_client.incr('attempts:' + key)
        if attempt_count >= 3:
            logger.exception(f"Error processing image {key}. Max attempts reached. Deleting image.")
            s3_client.delete_object(Bucket=bucket_name, Key=key)
            redis_client.delete('attempts:' + key)
            redis_client.delete('lock:' + key)
        else:
            logger.exception(f"Error processing image {key}. Retrying after 180 seconds.")
            redis_client.delete('lock:' + key)
            raise self.retry(exc=e, countdown=180)

@celery.task(bind=True)
def process_hero_stats_task(self, key, folder, hero_name):
    if key == "hero-stats/": return    
    global redis_client, bucket_name, boto3_config, api_key
    attempt_count = int(redis_client.get('attempts:' + key) or 0)
    # Check the attempt count    
    if attempt_count >= 3:
        logger.info(f"Image {key} has reached maximum attempts. Deleting image.")
        s3_client.delete_object(Bucket=bucket_name, Key=key)
        redis_client.delete('attempts:' + key)
        redis_client.delete('lock:' + key)
        return
    logger.info(f"Processing image: {key} from folder '{folder}' as hero stat information (attempt {attempt_count + 1})")
    try:        
        # Retrieve cached data
        cached_data = redis_client.get('hero_data')
        if cached_data is None:
            logger.warning("Hero data not found in cache.")
            return
        
        hero_data = json.loads(cached_data)
        hero = next((h for h in hero_data['data']['heroes']['nodes'] if h['slug'] == hero_name), None)

        if hero is None:
            logger.warning(f"Hero '{hero_name}' not found.")
            return

        s3_client = boto3.client('s3', **boto3_config)

        # Generate a pre-signed URL for the image    
        pre_signed_url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket_name, 'Key': key},
            ExpiresIn=3600
        )

        # AI processing: Preparing the AI payload
        messages = [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": stat_prompt,
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
            "Authorization": f"Bearer {api_key}"
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
            hero_stats = json.loads(cleaned_data)
            logger.info("Successfully processed JSON from AI response")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON: {e}")
            return

        # Example payload for hero stats from AI response
        payload = {
            "atk": hero_stats.get("atk", 0),
            "def": hero_stats.get("def", 0),
            "hp": hero_stats.get("hp", 0),
            "crit": hero_stats.get("crit", 0),
            "heal": hero_stats.get("heal", 0),
            "damage_reduction": hero_stats.get("damage_reduction", 0),
            "basic_resistance": hero_stats.get("basic_resistance", 0),
            "light_resistance": hero_stats.get("light_resistance", 0),
            "dark_resistance": hero_stats.get("dark_resistance", 0),
            "fire_resistance": hero_stats.get("fire_resistance", 0),
            "earth_resistance": hero_stats.get("earth_resistance", 0),
            "water_resistance": hero_stats.get("water_resistance", 0),
            "compatible_equipment": hero_stats.get("compatible_equipment", []),
            "passives": hero_stats.get("passives", [])
        }

        # Fix: Extract string values from passives if they are dictionaries
        passives_list = []
        if 'passives' in payload:
            passives_list = [
                f"{'[Party] ' if passive['affects_party'] else ''}{passive['stat']} +{passive['value']:.1f}%"
                for passive in payload['passives']
            ]

        # Prepare and send the poll to Discord
        embed_data = {
            "title": f"Hero Stats - {hero['title']}",
            "description": "Here's what I found in your image:",
            "color": 3447003,  # Example blue color
            "fields": [
                {"name": "Atk", "value": payload["atk"], "inline": True} if payload["atk"] != 0 else None,
                {"name": "Def", "value": payload["def"], "inline": True} if payload["def"] != 0 else None,
                {"name": "HP", "value": payload["hp"], "inline": True} if payload["hp"] != 0 else None,
                {"name": "Crit", "value": payload["crit"], "inline": True} if payload["crit"] != 0 else None,
                {"name": "Heal", "value": payload["heal"], "inline": True} if payload["heal"] != 0 else None,
                {"name": "Damage Reduction", "value": payload["damage_reduction"], "inline": True} if payload["damage_reduction"] != 0 else None,
                {"name": "Basic Resistance", "value": payload["basic_resistance"], "inline": True} if payload["basic_resistance"] != 0 else None,
                {"name": "Light Resistance", "value": payload["light_resistance"], "inline": True} if payload["light_resistance"] != 0 else None,
                {"name": "Dark Resistance", "value": payload["dark_resistance"], "inline": True} if payload["dark_resistance"] != 0 else None,
                {"name": "Fire Resistance", "value": payload["fire_resistance"], "inline": True} if payload["fire_resistance"] != 0 else None,
                {"name": "Earth Resistance", "value": payload["earth_resistance"], "inline": True} if payload["earth_resistance"] != 0 else None,
                {"name": "Water Resistance", "value": payload["water_resistance"], "inline": True} if payload["water_resistance"] != 0 else None,
                {"name": "Compatible Equipment", "value": "\n".join(payload["compatible_equipment"]), "inline": False},
                {"name": "Passives", "value": "\n".join(passives_list), "inline": False},
            ],
            "footer": {"text": "Does this look correct?"}
        }

        # Remove any None fields (in case some stats are not present)
        embed_data["fields"] = [field for field in embed_data["fields"] if field]

        # Send poll request to Discord through Redis
        poll_data = {
            'channel_id': current_app.config['DISCORD_CHANNEL_ID'], 
            'is_embed': True,
            'embed': embed_data,
            'task_id': process_hero_stats_task.request.id
        }
        redis_client.rpush('discord_message_queue', json.dumps(poll_data))
        logger.info(f"Sent poll to Discord for hero: {hero['title']}")

        # Wait for poll result (e.g., 60 seconds)
        result_key = f"discord_poll_result:{process_hero_stats_task.request.id}"
        upvotes, downvotes = 0, 0

        for _ in range(100):  # Check every second, up to 120 seconds
            poll_result = redis_client.get(result_key)
            if poll_result:
                poll_result_data = json.loads(poll_result)
                upvotes = poll_result_data.get('upvotes', 0)
                downvotes = poll_result_data.get('downvotes', 0)
                redis_client.delete(result_key)
                break
            time.sleep(1)

        logger.info("Checking poll results: Upvotes - %d, Downvotes - %d", upvotes, downvotes)
        
        update_url = f"{current_app.config['WORDPRESS_SITE']}/wp-json/heavenhold/v1/update-stats"
        
        # If upvotes are higher than downvotes, post the data to WordPress
        if upvotes > downvotes:
            response = requests.post(update_url, json={
                'hero_id': hero['databaseId'],
                'atk': payload['atk'],
                'def': payload['def'],
                'hp': payload['hp'],
                'crit': payload['crit'],
                'heal': payload['heal'],
                'damage_reduction': payload['damage_reduction'],
                'basic_resistance': payload['basic_resistance'],
                'light_resistance': payload['light_resistance'],
                'dark_resistance': payload['dark_resistance'],
                'fire_resistance': payload['fire_resistance'],
                'earth_resistance': payload['earth_resistance'],
                'water_resistance': payload['water_resistance'],
                'compatible_equipment': payload['compatible_equipment'],
                'passives': payload['passives'],
                'confirmed': True
            })
            response.raise_for_status()
            logger.info(f"Hero stats updated successfully for hero {hero['title']}")
        elif upvotes == 0 and downvotes == 0:
            response = requests.post(update_url, json={
                'hero_id': hero['databaseId'],
                'atk': payload['atk'],
                'def': payload['def'],
                'hp': payload['hp'],
                'crit': payload['crit'],
                'heal': payload['heal'],
                'damage_reduction': payload['damage_reduction'],
                'basic_resistance': payload['basic_resistance'],
                'light_resistance': payload['light_resistance'],
                'dark_resistance': payload['dark_resistance'],
                'fire_resistance': payload['fire_resistance'],
                'earth_resistance': payload['earth_resistance'],
                'water_resistance': payload['water_resistance'],
                'compatible_equipment': payload['compatible_equipment'],
                'passives': payload['passives'],
                'confirmed': False
            })
            response.raise_for_status()
            logger.info(f"Hero stats updated successfully for hero {hero['title']}")
        else:
            logger.info(f"Aborting stat update for {hero['title']}")
        # Delete the image after processing (if desired)
        s3_client.delete_object(Bucket=bucket_name, Key=key)
        redis_client.delete('attempts:' + key)
        redis_client.delete('lock:' + key)
        logger.info(f"{key} processed successfully, deleting from S3 bucket.")
    except Exception as e:
        # Increment the attempt count
        attempt_count = redis_client.incr('attempts:' + key)
        if attempt_count >= 3:
            logger.exception(f"Error processing image {key}. Max attempts reached. Deleting image.")
            s3_client.delete_object(Bucket=bucket_name, Key=key)
            redis_client.delete('attempts:' + key)
            redis_client.delete('lock:' + key)
        else:
            logger.exception(f"Error processing image {key}. Retrying after 180 seconds.")
            redis_client.delete('lock:' + key)
            raise self.retry(exc=e, countdown=180)
        
@celery.task(bind=True)
def process_weapon_information_task(self, key, folder, item_name):
    if key == "weapon-information/": return    
    global redis_client, bucket_name, boto3_config, api_key
    attempt_count = int(redis_client.get('attempts:' + key) or 0)
    # Check the attempt count    
    if attempt_count >= 3:
        logger.info(f"Image {key} has reached maximum attempts. Deleting image.")
        s3_client.delete_object(Bucket=bucket_name, Key=key)
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
        item = next((i for i in item_data['data']['item']['nodes'] if i['title'] == item_name), None)
        new_item = False
        if item is None:
            logger.warning(f"Item '{item_name}' not found, creating a new item.")
            new_item = True


        s3_client = boto3.client('s3', **boto3_config)

        # Generate a pre-signed URL for the image    
        pre_signed_url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket_name, 'Key': key},
            ExpiresIn=3600
        )

        # AI processing: Preparing the AI payload
        messages = [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                    "text": weapon_prompt + json.dumps(item_data),
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
            "Authorization": f"Bearer {api_key}"
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
            "wepaon_skill_regen_time": item_info.get("weapon_skill_regen_time", 0),
            "weapon_skill_description": item_info.get("weapon_skill_description", 0),
            "weapon_skill_chain": item_info.get("weapon_skill_chain", 0),
            "main_option": item_info.get("main_option", []),
            "sub_option": item_info.get("sub_option", []),
            "limit_break_5_option": item_info.get("limit_break_5_option", 0),
            "limit_break_5_value": item_info.get("limit_break_5_value", 0),
            "engraving_options": item_info.get("engraving_options", []),
        }

        # Prepare and send the poll to Discord
        embed_data = {
            "title": f"Item Information - {item['title']}",
            "description": "Here's what I found in your image:",
            "color": 3447003,  # Example blue color
            "fields": [
                {"name": "Name", "value": payload["name"], "inline": True} if payload["name"] != 0 else None,
                {"name": "Rarity", "value": payload["rarity"], "inline": True} if payload["rarity"] != 0 else None,
                {"name": "Weapon Type", "value": payload["weapon_type"], "inline": True} if payload["weapon_type"] != 0 else None,
                {"name": "Exclusive", "value": payload["exclusive"], "inline": True} if payload["exclusive"] != 0 else None,
                {"name": "Hero", "value": payload["hero"], "inline": True} if payload["hero"] != 0 else None,
                {"name": "Exclusive Effects", "value": payload["exclusive_effects"], "inline": True} if payload["exclusive_effects"] != 0 else None,
                {"name": "Min DPS", "value": payload["min_dps"], "inline": True} if payload["min_dps"] != 0 else None,
                {"name": "Max DPS", "value": payload["max_dps"], "inline": True} if payload["max_dps"] != 0 else None,
                {"name": "Weapon Skill Name", "value": payload["weapon_skill_name"], "inline": True} if payload["weapon_skill_name"] != 0 else None,
                {"name": "Weapon Skill Atk", "value": payload["weapon_skill_atk"], "inline": True} if payload["weapon_skill_atk"] != 0 else None,
                {"name": "Weapon Skill Regen Time", "value": payload["weapon_skill_regen_time"], "inline": True} if payload["weapon_skill_regen_time"] != 0 else None,
                {"name": "Weapon Skill Description", "value": payload["weapon_skill_description"], "inline": True} if payload["weapon_skill_description"] != 0 else None,
                {"name": "Weapon Skill Chain", "value": "\n".join(payload["weapon_skill_chain"]), "inline": False} if payload["weapon_skill_chain"] != 0 else None,                
                {"name": "Main Option", "value": "\n".join(payload["main_option"]), "inline": False},
                {"name": "Sub Option", "value": "\n".join(payload["sub_option"]), "inline": False},
                {"name": "Engraving Options", "value": "\n".join(payload["engraving_options"]), "inline": False},
                {"name": "Limit Break 5 Option", "value": "\n".join(payload["limit_break_5_option"]), "inline": False} if payload["limit_break_5_option"] != 0 else None,
                {"name": "Limit Break 5 Value", "value": "\n".join(payload["limit_break_5_value"]), "inline": False} if payload["limit_break_5_value"] != 0 else None,
            ],
            "footer": {"text": "Does this look correct?"}
        }

        # Remove any None fields (in case some stats are not present)
        embed_data["fields"] = [field for field in embed_data["fields"] if field]

        # Send poll request to Discord through Redis
        poll_data = {
            'channel_id': current_app.config['DISCORD_CHANNEL_ID'], 
            'is_embed': True,
            'embed': embed_data,
            'task_id': process_weapon_information_task.request.id
        }
        redis_client.rpush('discord_message_queue', json.dumps(poll_data))
        logger.info(f"Sent poll to Discord for hero: {item['title']}")

        # Wait for poll result (e.g., 60 seconds)
        result_key = f"discord_poll_result:{process_weapon_information_task.request.id}"
        upvotes, downvotes = 0, 0

        for _ in range(100):  # Check every second, up to 120 seconds
            poll_result = redis_client.get(result_key)
            if poll_result:
                poll_result_data = json.loads(poll_result)
                upvotes = poll_result_data.get('upvotes', 0)
                downvotes = poll_result_data.get('downvotes', 0)
                redis_client.delete(result_key)
                break
            time.sleep(1)

        logger.info("Checking poll results: Upvotes - %d, Downvotes - %d", upvotes, downvotes)
        
        update_url = f"{current_app.config['WORDPRESS_SITE']}/wp-json/heavenhold/v1/update-weapon"
        
        # If upvotes are higher than downvotes, post the data to WordPress
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
                "wepaon_skill_regen_time": payload.get("weapon_skill_regen_time", 0),
                "weapon_skill_description": payload.get("weapon_skill_description", 0),
                "weapon_skill_chain": payload.get("weapon_skill_chain", 0),
                "main_option": payload.get("main_option", []),
                "sub_option": payload.get("sub_option", []),
                "limit_break_5_option": payload.get("limit_break_5_option", 0),
                "limit_break_5_value": payload.get("limit_break_5_value", 0),
                "engraving_options": payload.get("engraving_options", []),               
                'confirmed': True
            })
            response.raise_for_status()
            logger.info(f"Hero stats updated successfully for weapon {item['title']}")
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
                "wepaon_skill_regen_time": payload.get("weapon_skill_regen_time", 0),
                "weapon_skill_description": payload.get("weapon_skill_description", 0),
                "weapon_skill_chain": payload.get("weapon_skill_chain", 0),
                "main_option": payload.get("main_option", []),
                "sub_option": payload.get("sub_option", []),
                "limit_break_5_option": payload.get("limit_break_5_option", 0),
                "limit_break_5_value": payload.get("limit_break_5_value", 0),
                "engraving_options": payload.get("engraving_options", []),               
                'confirmed': False
            })
            response.raise_for_status()
            logger.info(f"Hero stats updated successfully for weapon {item['title']}")
        else:
            logger.info(f"Aborting stat update for {item['title']}")
        # Delete the image after processing (if desired)
        s3_client.delete_object(Bucket=bucket_name, Key=key)
        redis_client.delete('attempts:' + key)
        redis_client.delete('lock:' + key)
        logger.info(f"{key} processed successfully, deleting from S3 bucket.")
    except Exception as e:
        # Increment the attempt count
        attempt_count = redis_client.incr('attempts:' + key)
        if attempt_count >= 3:
            logger.exception(f"Error processing image {key}. Max attempts reached. Deleting image.")
            s3_client.delete_object(Bucket=bucket_name, Key=key)
            redis_client.delete('attempts:' + key)
            redis_client.delete('lock:' + key)
        else:
            logger.exception(f"Error processing image {key}. Retrying after 180 seconds.")
            redis_client.delete('lock:' + key)
            raise self.retry(exc=e, countdown=180)

@celery.task
def check_and_process_s3_images(folder):
    global redis_client, bucket_name, boto3_config
    try:
        # Initialize S3 client using app.config variables
        s3_client = boto3.client('s3', **boto3_config)

        # Check for images in the specified S3 bucket folder
        response = s3_client.list_objects_v2(
            Bucket=bucket_name,
            Prefix=folder
        )

        if 'Contents' in response:
            for obj in response['Contents']:
                key = obj['Key']
                if key != folder + '/':
                    # Get the attempt count
                    attempt_count = int(redis_client.get('attempts:' + key) or 0)
                    if attempt_count >= 3:
                        logger.info(f"Image {key} has reached maximum attempts. Deleting image.")
                        # Delete image from S3
                        s3_client.delete_object(Bucket=bucket_name, Key=key)
                        # Delete the attempt counter
                        redis_client.delete('attempts:' + key)
                        continue  # Skip to the next image

                    # Try to acquire the lock
                    lock_acquired = redis_client.set('lock:' + key, 1, nx=True, ex=600)  # Lock expires in 600 seconds
                    if not lock_acquired:
                        logger.info(f"Image {key} is already being processed by another node. Skipping.")
                        continue  # Skip to the next image
                    logger.info(f"Found new image: {key}, adding to processing queue.")
                    # Extract hero_name from key
                    filename = key.split('/')[-1]
                    filename_without_extension = filename.split('.')[0]
                    hero_name_parts = filename_without_extension.split('_')
                    if len(hero_name_parts) >= 2:
                        hero_name = hero_name_parts[0]
                        # Call the appropriate task
                        if folder == "hero-stories":
                            process_hero_story_task.delay(key, folder, hero_name)
                        elif folder == "hero-portraits":
                            region = hero_name_parts[1]
                            process_hero_portrait_task.delay(key, folder, hero_name, region)
                        elif folder == "hero-illustrations":
                            region = hero_name_parts[1]
                            process_hero_illustration_task.delay(key, folder, hero_name, region)
                        elif folder == "hero-bios":                        
                            process_hero_bio_task.delay(key, folder, hero_name)
                        elif folder == "hero-stats":                        
                            process_hero_stats_task.delay(key, folder, hero_name)
                        elif folder == "weapon-information":
                            process_weapon_information_task.delay(key, folder, hero_name)
                    elif filename != '':
                        logger.warning(f"Invalid filename format: {filename}. Skipping processing.")
        else:
            logger.info(f"No images found in the S3 bucket folder '{folder}'.")
    except Exception as e:
        logger.exception(f"Error checking S3 bucket folder '{folder}': {e}")

def detect_black_bar_width(image_path, threshold=10, black_threshold=50):
    # Open image and convert to grayscale
    img = Image.open(image_path).convert('L')  # Convert to grayscale ('L' mode)
    img_array = np.array(img)

    height, width = img_array.shape

    def detect_black_bar_from_edge(edge_pixels):
        """ Helper function to detect the width of the black bar on one side. """
        black_bar_width = 0
        consecutive_non_black_rows = 0

        # Traverse pixels from the edge towards the center
        for i in range(width):
            # Check if the entire column of pixels is black
            if np.all(edge_pixels[:, i] < black_threshold):
                black_bar_width += 1
            else:
                consecutive_non_black_rows += 1
                if consecutive_non_black_rows >= threshold:
                    break
        return black_bar_width

    # Get pixel columns for the left and right sides of the image
    left_edge = img_array[:, :width//2]  # Left half of the image
    right_edge = img_array[:, width//2:]  # Right half of the image

    # Detect black bars on both sides
    left_black_bar_width = detect_black_bar_from_edge(left_edge)
    right_black_bar_width = detect_black_bar_from_edge(np.fliplr(right_edge))  # Flip for right side detection

    return left_black_bar_width, right_black_bar_width

def encode_image_to_base64(image_content):
    return base64.b64encode(image_content).decode('utf-8')

def handle_expired_keys():
    r = redis.Redis(host='redis-service', port=6379, db=0)
    pubsub = r.pubsub()
    pubsub.subscribe('__keyevent@0__:expired')

    for message in pubsub.listen():
        if message['type'] == 'message':
            expired_key = message['data'].decode('utf-8')
            if expired_key.startswith('processing:'):
                key = expired_key[len('processing:'):]
                # Call the function you want to execute
                on_key_expired(key)

def on_key_expired(key):
    logger.info(f"Key {key} has expired after 180 seconds.")

threading.Thread(target=handle_expired_keys, daemon=True).start()