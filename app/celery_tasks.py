# app/celery_tasks.py

import logging
import time
import boto3
import os
import base64
import io
import tempfile
from flask import current_app
from .app import celery
from .stat_prompt import stat_prompt
from .assistant_prompt import system_prompt
from .hero_query import hero_query
import requests
import json
import redis
import numpy as np
from PIL import Image

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s: %(levelname)s/%(processName)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

redis_client = redis.Redis(host='redis-service', port=6379, db=0)

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
        name="Fetch hero stories data from WordPress",
        countdown=0  # No delay needed
    )

    # Trigger tasks immediately on startup
    fetch_hero_data.delay()


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
def process_hero_story_task(key, folder, hero_name):
    if key == "hero-stories/": return
    logger.info(f"Processing image: {key} from folder '{folder}' as a hero story")

    try:
        # Initialize Redis client
        global redis_client
        api_key = current_app.config['OPENAI_API_KEY']

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
        s3_client = boto3.client(
            's3',
            aws_access_key_id=current_app.config['AWS_ACCESS_KEY_ID'],
            aws_secret_access_key=current_app.config['AWS_SECRET_ACCESS_KEY'],
            region_name=current_app.config['AWS_REGION'],
        )

        # Generate a pre-signed URL for the image
        bucket_name = current_app.config['AWS_S3_BUCKET']
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
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
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
            logger.info(f"{key} processed successfully, deleting from S3 bucket.")
        except json.JSONDecodeError as e:
            logger.error("Failed to parse JSON from AI response")
            logger.error(e)
            # Handle the error accordingly

    except Exception as e:
        logger.exception(f"Error processing image {key} from folder '{folder}':")

@celery.task
def process_hero_portrait_task(key, folder, hero_name, region):
    if key == "hero-portraits/": return
    logger.info(f"Processing image: {key} from folder '{folder}' as a hero portrait")

    try:
        # Initialize Redis client
        global redis_client
        api_key = current_app.config['OPENAI_API_KEY']

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
        s3_client = boto3.client(
            's3',
            aws_access_key_id=current_app.config['AWS_ACCESS_KEY_ID'],
            aws_secret_access_key=current_app.config['AWS_SECRET_ACCESS_KEY'],
            region_name=current_app.config['AWS_REGION'],
        )

        # Retrieve and process the image from S3
        response = s3_client.get_object(
            Bucket=current_app.config['AWS_S3_BUCKET'],
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

            # Convert the cropped image to a byte stream
            img_byte_arr = io.BytesIO()
            cropped_img.save(img_byte_arr, format='JPEG')
            img_byte_arr.seek(0)

            # POST the image 
            try:                
                logger.info("Successfully cropped portrait image")
                # POST the hero's story and ID to the specified URL
                files = {
                    'image': (hero_name + '.jpg', img_byte_arr, 'image/jpeg') 
                }
                payload = {
                    'hero_id': hero['databaseId'],
                    'region': region,
                }

                # Prepare and send the poll to Discord
                embed_data = {
                    "title": f"Hero Portrait - {hero['title']}",
                    "description": "I did my best!",
                    "color": 3447003,  # Example blue color
                    "fields": [
                        {"name": "Region", "value": payload["region"], "inline": True} if payload["region"] != 0 else None,                        
                    ],
                    "footer": {"text": "Does this look correct?"}
                }

                # Remove any None fields (in case some stats are not present)
                embed_data["fields"] = [field for field in embed_data["fields"] if field]

                # Convert the image to Base64
                base64_image = encode_image_to_base64(cropped_img)
                
                # Send poll request to Discord through Redis
                poll_data = {
                    'channel_id': current_app.config['DISCORD_CHANNEL_ID'], 
                    'is_embed': True,
                    'embed': embed_data,
                    'image': base64_image,
                    'filename': hero_name + '.png',
                    'task_id': process_hero_portrait_task.request.id
                }
                redis_client.rpush('discord_message_queue', json.dumps(poll_data))
                logger.info(f"Sent poll to Discord for hero: {hero['title']}")
                
                # Wait for poll result (e.g., 60 seconds)
                result_key = f"discord_poll_result:{process_hero_portrait_task.request.id}"
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

                update_url = current_app.config['WORDPRESS_SITE'] + '/wp-json/heavenhold/v1/update-portrait'

                # If upvotes are higher than downvotes, post the data to WordPress
                if upvotes > downvotes:
                    response = requests.post(update_url, files=files, data={
                        'hero_id': hero['databaseId'],
                        'region': region,
                        'confirmed': True
                    })
                    response.raise_for_status()
                    logger.info("Hero portrait updated successfully")
                elif upvotes == 0 and downvotes == 0:
                    response = requests.post(update_url, files=files, data={
                        'hero_id': hero['databaseId'],
                        'region': region,
                        'confirmed': False
                    })
                    response.raise_for_status()
                    logger.info("Hero portrait updated successfully")
                else:
                    logger.info(f"Aborting portrait update for {hero['title']}")

                # Delete the image after processing (if desired)
                s3_client.delete_object(Bucket=current_app.config['AWS_S3_BUCKET'], Key=key)                    
                fetch_hero_data.delay()
                logger.info(f"{key} processed successfully, deleting from S3 bucket.")
            except json.JSONDecodeError as e:
                logger.error("Failed to parse JSON from AI response")
                logger.error(e)
                # Handle the error accordingly

    except Exception as e:
        logger.exception(f"Error processing image {key} from folder '{folder}':")

@celery.task
def process_hero_illustration_task(key, folder, hero_name, region):
    if key == "hero-illustrations/": return
    logger.info(f"Processing image: {key} from folder '{folder}' as a hero illustration and thumbnail")

    try:
        # Initialize Redis client
        global redis_client
        api_key = current_app.config['OPENAI_API_KEY']

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
        s3_client = boto3.client(
            's3',
            aws_access_key_id=current_app.config['AWS_ACCESS_KEY_ID'],
            aws_secret_access_key=current_app.config['AWS_SECRET_ACCESS_KEY'],
            region_name=current_app.config['AWS_REGION'],
        )

        # Generate a pre-signed URL for the image
        bucket_name = current_app.config['AWS_S3_BUCKET']
        pre_signed_url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket_name, 'Key': key},
            ExpiresIn=3600  
        )

        # Retrieve and process the image from S3
        s3_response = s3_client.get_object(
            Bucket=current_app.config['AWS_S3_BUCKET'],
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
            response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
            )

            # Parse the JSON content
            response.raise_for_status()
            response_json = response.json()            

            # Access the 'choices' data
            extracted_data = response_json['choices'][0]['message']['content']
            cleaned_data = extracted_data.strip('```json').strip('```')

            # Log the response JSON
            logger.info(cleaned_data)
            
            # Convert image to a byte stream
            img_byte_arr = io.BytesIO()
            original_img.save(img_byte_arr, format='PNG')
            img_byte_arr.seek(0)
            
            # Attempt to parse the extracted data as JSON
            try:  
                crop_data = json.loads(cleaned_data)
                
                # Prepare the form-data payload (all data must be form fields)
                files = {
                    'image': (hero_name + '.png', img_byte_arr, 'image/png')
                }
                # Sending the rest of the form data as part of `data`
                payload = {
                    'hero_id': hero.get('databaseId', 0),
                    'region': region,
                    'x': crop_data.get('x', 0),
                    'y': crop_data.get('y', 0),
                    'width': crop_data.get('width', 0),
                    'height': crop_data.get('height', 0),
                }

                # Prepare and send the poll to Discord
                embed_data = {
                    "title": f"Hero Illustration - {hero['title']}",
                    "description": "Here's what you gave me:",
                    "color": 3447003,  # Example blue color
                    "fields": [
                        {"name": "Region", "value": payload["region"], "inline": True} if payload["region"] != 0 else None,                        
                    ],
                    "footer": {"text": "Does this look correct?"}
                }

                # Remove any None fields (in case some stats are not present)
                embed_data["fields"] = [field for field in embed_data["fields"] if field]

                # Convert the image to Base64
                base64_image = encode_image_to_base64(image_content)

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
                
                # Wait for poll result (e.g., 60 seconds)
                result_key = f"discord_poll_result:{process_hero_illustration_task.request.id}"
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
                
                # Send the POST request with form-data
                update_url = current_app.config['WORDPRESS_SITE'] + '/wp-json/heavenhold/v1/update-illustration'

                # If upvotes are higher than downvotes, post the data to WordPress
                if upvotes > downvotes:
                    response = requests.post(update_url, files=files, data={
                        'hero_id': hero['databaseId'],
                        'region': region,
                        'x': crop_data['x'],
                        'y': crop_data['y'],
                        'width': crop_data['width'],
                        'height': crop_data['height'],
                        'confirmed': True
                    })
                    # Raise an exception if the response contains an error
                    response.raise_for_status()
                    logger.info("Hero illustration/thumbnail updated successfully")
                elif upvotes == 0 and downvotes == 0:
                    response = requests.post(update_url, files=files, data={
                        'hero_id': payload['hero_id'],
                        'region': payload['region'],
                        'x': payload['x'],
                        'y': payload['y'],
                        'width': payload['width'],
                        'height': payload['height'],
                        'confirmed': False
                    })
                    # Raise an exception if the response contains an error
                    response.raise_for_status()
                    logger.info("Hero illustration/thumbnail updated successfully")
                else:
                    logger.info(f"Aborting illustration/thumbnail update for {hero['title']}")
                
                # Optionally delete the image after processing
                s3_client.delete_object(Bucket=current_app.config['AWS_S3_BUCKET'], Key=key)
                logger.info(f"{key} processed successfully, deleting from S3 bucket.")
            except json.JSONDecodeError as e:
                logger.error("Failed to parse JSON from AI response")
                logger.error(e)
                # Handle the error accordingly

    except Exception as e:
        logger.exception(f"Error processing image {key} from folder '{folder}':")

@celery.task
def process_hero_bio_task(key, folder, hero_name):
    if key == "hero-bios/": return
    logger.info(f"Processing image: {key} from folder '{folder}' as hero bio information.")

    try:
        # Initialize Redis client
        global redis_client
        api_key = current_app.config['OPENAI_API_KEY']

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
        s3_client = boto3.client(
            's3',
            aws_access_key_id=current_app.config['AWS_ACCESS_KEY_ID'],
            aws_secret_access_key=current_app.config['AWS_SECRET_ACCESS_KEY'],
            region_name=current_app.config['AWS_REGION'],
        )

        # Generate a pre-signed URL for the image
        bucket_name = current_app.config['AWS_S3_BUCKET']
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
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
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
            logger.info(f"{key} processed successfully, deleting from S3 bucket.")
        except json.JSONDecodeError as e:
            logger.error("Failed to parse JSON from AI response")
            logger.error(e)
            # Handle the error accordingly

    except Exception as e:
        logger.exception(f"Error processing image {key} from folder '{folder}':")

@celery.task
def process_hero_stats_task(key, folder, hero_name):
    if key == "hero-stats/": return
    logger.info(f"Processing image: {key} from folder '{folder}' as hero stat information.")
    
    try:
        global redis_client
        api_key = current_app.config['OPENAI_API_KEY']
        
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

        # Generate a pre-signed URL for the image
        s3_client = boto3.client(
            's3',
            aws_access_key_id=current_app.config['AWS_ACCESS_KEY_ID'],
            aws_secret_access_key=current_app.config['AWS_SECRET_ACCESS_KEY'],
            region_name=current_app.config['AWS_REGION'],
        )
        bucket_name = current_app.config['AWS_S3_BUCKET']
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
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
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
        logger.info(f"{key} processed successfully, deleting from S3 bucket.")
    except Exception as e:
        logger.exception(f"Error processing hero stats: {e}")

@celery.task
def check_and_process_s3_images(folder):
    logger.info(f"Checking for new images in S3 bucket folder '{folder}'")
    try:
        # Initialize S3 client using app.config variables
        s3_client = boto3.client(
            's3',
            aws_access_key_id=current_app.config['AWS_ACCESS_KEY_ID'],
            aws_secret_access_key=current_app.config['AWS_SECRET_ACCESS_KEY'],
            region_name=current_app.config['AWS_REGION'],
        )

        # Check for images in the specified S3 bucket folder
        response = s3_client.list_objects_v2(
            Bucket=current_app.config['AWS_S3_BUCKET'],
            Prefix=folder
        )

        if 'Contents' in response:
            if len(response['Contents']) > 1:
                logger.info(f"Found {len(response['Contents'])} items in S3 bucket folder '{folder}'.")

            for obj in response['Contents']:
                key = obj['Key']
                if key != folder + '/':
                    # Check if the image has already been processed
                    if redis_client.sismember('processed_images', key):
                        logger.info(f"Image {key} has already been processed. Skipping processing.")
                        continue

                    logger.info(f"Found image: {key}, adding to processing queue.")
                    # Extract hero_name from key
                    filename = key.split('/')[-1]
                    filename_without_extension = filename.split('.')[0]
                    hero_name_parts = filename_without_extension.split('_')
                    if len(hero_name_parts) >= 2:
                        hero_name = hero_name_parts[0]
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
                        else:
                            process_image_task.delay(key, folder)
                        
                        # Add the processed image to a set in Redis
                        redis_client.sadd('processed_images', key)
                    elif filename != '':
                        logger.warning(f"Invalid filename format: {filename}. Skipping processing.")
        else:
            logger.info(f"No images found in the S3 bucket folder '{folder}'.")
    except Exception as e:
        logger.exception(f"Error checking S3 bucket folder '{folder}': {e}")

@celery.task
def process_image_task(key, folder):
    logger.info(f"Processing image: {key} from folder '{folder}'")
    try:
        # Initialize S3 client using app.config variables
        s3_client = boto3.client(
            's3',
            aws_access_key_id=current_app.config['AWS_ACCESS_KEY_ID'],
            aws_secret_access_key=current_app.config['AWS_SECRET_ACCESS_KEY'],
            region_name=current_app.config['AWS_REGION'],
        )

        # Retrieve and process the image from S3
        response = s3_client.get_object(
            Bucket=current_app.config['AWS_S3_BUCKET'],
            Key=key
        )
        file_content = response['Body'].read()

        # Delete the image after processing (if desired)
        # s3_client.delete_object(Bucket=current_app.config['AWS_S3_BUCKET'], Key=key)
        logger.info(f"Image {key} processed successfully.")
    except Exception as e:
        logger.exception(f"Error processing image {key} from folder '{folder}':")

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