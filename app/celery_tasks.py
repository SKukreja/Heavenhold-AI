# app/celery_tasks.py

import logging
import boto3
import os
import io
import tempfile
from flask import current_app
from .app import celery
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

# Set up periodic tasks
@celery.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs):
    # Schedule to check 'hero-stories' folder every 30 seconds
    sender.add_periodic_task(
        60.0,  # Run every 30 seconds (adjust as needed)
        check_and_process_s3_images.s('hero-stories'),
        name="Check 'hero-stories' folder in S3 bucket",
    )

    sender.add_periodic_task(
        60.0,  # Run every 30 seconds (adjust as needed)
        check_and_process_s3_images.s('hero-portraits'),
        name="Check 'hero-portraits' folder in S3 bucket",
    )

    sender.add_periodic_task(
        60.0,  # Run every 30 seconds (adjust as needed)
        check_and_process_s3_images.s('hero-illustrations'),
        name="Check 'hero-illustrations' folder in S3 bucket",
    )

    sender.add_periodic_task(
        60.0,  # Run every 30 seconds (adjust as needed)
        check_and_process_s3_images.s('hero-bios'),
        name="Check 'hero-bios' folder in S3 bucket",
    )

    # Schedule to fetch hero stories data every 10 minutes
    sender.add_periodic_task(
        180.0,  # Run every 3 minutes
        fetch_hero_data.s(),
        name="Fetch hero stories data from WordPress",
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
        redis_client = redis.Redis(host='redis-service', port=6379, db=0)
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
        redis_client = redis.Redis(host='redis-service', port=6379, db=0)

        api_key = current_app.config['OPENAI_API_KEY']

        # Retrieve cached data
        cached_data = redis_client.get('hero_data')
        if cached_data is not None:
            hero_data = json.loads(cached_data)
            logger.info("Retrieved hero data from cache.")

            # Extract the list of heroes
            heroes_list = hero_data['data']['heroes']['nodes']

            db_hero_data = None

            # Search for the hero with matching slug
            for hero in heroes_list:
                if hero['slug'] == hero_name:
                    db_hero_data = hero
                    break

            if db_hero_data is not None:
                # Now test_hero_data contains the data for the hero named test_hero
                logger.info(f"Found hero data for {hero_name}")
                # Proceed with using test_hero_data as needed
            else:
                logger.warning(f"Hero '{hero_name}' not found in hero data.")
                return  # Exit the task if the hero is not found

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
                            "text": "Please analyze this image and generate a JSON object containing only the updated story for this hero. The hero's story should be recorded exactly as written, but you will only receive part of it on each screenshot. Check the current data for this hero to see if part of the story has already been recorded, and then append or prepend the new parts you see in the image, piecing together as much of the full story as you can in your output. If the current story already has more than what you see, do not change it. Respond with only valid JSON data to import. Current data for this hero: " + json.dumps(db_hero_data['heroInformation']['bioFields']),
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
            response_json = response.json()            

            # Check for errors
            if 'error' in response_json:
                logger.error(f"OpenAI API error: {response_json['error']}")
                return

            # Access the 'choices' data
            extracted_data = response_json['choices'][0]['message']['content']
            cleaned_data = extracted_data.strip('```json').strip('```')

            # Log the response JSON
            logger.info(cleaned_data)

            # Attempt to parse the extracted data as JSON
            try:                
                hero_data = json.loads(cleaned_data)
                logger.info("Successfully processed JSON from AI response")
                logger.info(hero_data['story'])
                # POST the hero's story and ID to the specified URL
                update_url = current_app.config['WORDPRESS_SITE'] + '/wp-json/heavenhold/v1/update-story'
                payload = {
                    'hero_id': db_hero_data['databaseId'],
                    'story': hero_data['story'],
                }
                response = requests.post(update_url, json=payload)
                response.raise_for_status()
                logger.info("Hero story updated successfully")
                
                # Delete the image after processing (if desired)
                s3_client.delete_object(Bucket=bucket_name, Key=key)
                fetch_hero_data.delay()
                logger.info(f"{key} processed successfully, deleting from S3 bucket.")
            except json.JSONDecodeError as e:
                logger.error("Failed to parse JSON from AI response")
                logger.error(e)
                # Handle the error accordingly
        else:
            logger.warning("Hero data not found in cache")

    except Exception as e:
        logger.exception(f"Error processing image {key} from folder '{folder}':")

@celery.task
def process_hero_portrait_task(key, folder, hero_name, region):
    if key == "hero-portraits/": return
    logger.info(f"Processing image: {key} from folder '{folder}' as a hero portrait")
    try:
        # Initialize Redis client
        redis_client = redis.Redis(host='redis-service', port=6379, db=0)

        api_key = current_app.config['OPENAI_API_KEY']

        # Retrieve cached data
        cached_data = redis_client.get('hero_data')
        if cached_data is not None:
            hero_data = json.loads(cached_data)
            logger.info("Retrieved hero data from cache.")

            # Extract the list of heroes
            heroes_list = hero_data['data']['heroes']['nodes']

            db_hero_data = None

            # Search for the hero with name matching test_hero
            for hero in heroes_list:
                if hero['slug'] == hero_name:
                    db_hero_data = hero
                    break

            if db_hero_data is not None:
                # Now test_hero_data contains the data for the hero named test_hero
                logger.info(f"Found hero data for {hero_name}")
                # Proceed with using test_hero_data as needed
            else:
                logger.warning(f"Hero '{hero_name}' not found in hero data.")
                return  # Exit the task if the hero is not found

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
                    logger.info("Successfully processed portrait image")
                    # POST the hero's story and ID to the specified URL
                    update_url = current_app.config['WORDPRESS_SITE'] + '/wp-json/heavenhold/v1/update-portrait'
                    files = {
                        'image': (hero_name + '.jpg', img_byte_arr, 'image/jpeg') 
                    }
                    payload = {
                        'hero_id': db_hero_data['databaseId'],
                        'region': region,
                    }
                    response = requests.post(update_url, files=files, data=payload)
                    response.raise_for_status()
                    logger.info("Hero portrait updated successfully")
                    bucket_name = current_app.config['AWS_S3_BUCKET']

                    # Delete the image after processing (if desired)
                    s3_client.delete_object(Bucket=bucket_name, Key=key)                    
                    fetch_hero_data.delay()
                    logger.info(f"{key} processed successfully, deleting from S3 bucket.")
                except json.JSONDecodeError as e:
                    logger.error("Failed to parse JSON from AI response")
                    logger.error(e)
                    # Handle the error accordingly

        else:
            logger.warning("Hero data not found in cache")

    except Exception as e:
        logger.exception(f"Error processing image {key} from folder '{folder}':")

@celery.task
def process_hero_illustration_task(key, folder, hero_name, region):
    if key == "hero-illustrations/": return
    logger.info(f"Processing image: {key} from folder '{folder}' as a hero illustration and thumbnail")
    try:
        # Initialize Redis client
        redis_client = redis.Redis(host='redis-service', port=6379, db=0)

        api_key = current_app.config['OPENAI_API_KEY']

        # Retrieve cached data
        cached_data = redis_client.get('hero_data')
        if cached_data is not None:
            hero_data = json.loads(cached_data)
            logger.info("Retrieved hero data from cache.")

            # Extract the list of heroes
            heroes_list = hero_data['data']['heroes']['nodes']

            db_hero_data = None

            # Search for the hero with matching slug
            for hero in heroes_list:
                if hero['slug'] == hero_name:
                    db_hero_data = hero
                    break

            if db_hero_data is not None:
                # Now test_hero_data contains the data for the hero named test_hero
                logger.info(f"Found hero data for {hero_name}")
                # Proceed with using test_hero_data as needed
            else:
                logger.warning(f"Hero '{hero_name}' not found in hero data.")
                return  # Exit the task if the hero is not found

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
                response_json = response.json()            

                # Check for errors
                if 'error' in response_json:
                    logger.error(f"OpenAI API error: {response_json['error']}")
                    return

                # Access the 'choices' data
                extracted_data = response_json['choices'][0]['message']['content']
                cleaned_data = extracted_data.strip('```json').strip('```')

                # Log the response JSON
                logger.info(cleaned_data)
                
                # Convert image to a byte stream
                img_byte_arr = io.BytesIO()
                original_img.save(img_byte_arr, format='PNG')
                img_byte_arr.seek(0)
                
                # Assuming crop_data has been fetched successfully from AI
                crop_data = json.loads(cleaned_data)
                
                # Prepare the form-data payload (all data must be form fields)
                files = {
                    'image': (hero_name + '.png', img_byte_arr, 'image/png')
                }
                # Sending the rest of the form data as part of `data`
                payload = {
                    'hero_id': str(db_hero_data['databaseId']),
                    'region': region,
                    'x': str(crop_data['x']),
                    'y': str(crop_data['y']),
                    'width': str(crop_data['width']),
                    'height': str(crop_data['height']),
                }
                
                # Send the POST request with form-data
                update_url = current_app.config['WORDPRESS_SITE'] + '/wp-json/heavenhold/v1/update-illustration'
                response = requests.post(update_url, files=files, data=payload)
                
                # Raise an exception if the response contains an error
                response.raise_for_status()
                logger.info("Hero illustration/thumbnail updated successfully")
                
                # Optionally delete the image after processing
                s3_client.delete_object(Bucket=current_app.config['AWS_S3_BUCKET'], Key=key)
                fetch_hero_data.delay()
                logger.info(f"{key} processed successfully, deleting from S3 bucket.")
        else:
            logger.warning("Hero data not found in cache")

    except Exception as e:
        logger.exception(f"Error processing image {key} from folder '{folder}':")

@celery.task
def process_hero_bio_task(key, folder, hero_name):
    if key == "hero-bios/": return
    logger.info(f"Processing image: {key} from folder '{folder}' as hero bio information.")
    try:
        # Initialize Redis client
        redis_client = redis.Redis(host='redis-service', port=6379, db=0)

        api_key = current_app.config['OPENAI_API_KEY']

        # Retrieve cached data
        cached_data = redis_client.get('hero_data')
        if cached_data is not None:
            hero_data = json.loads(cached_data)
            logger.info("Retrieved hero data from cache.")

            # Extract the list of heroes
            heroes_list = hero_data['data']['heroes']['nodes']

            db_hero_data = None

            # Search for the hero with matching slug
            for hero in heroes_list:
                if hero['slug'] == hero_name:
                    db_hero_data = hero
                    break

            if db_hero_data is not None:
                # Now test_hero_data contains the data for the hero named test_hero
                logger.info(f"Found hero data for {hero_name}")
                # Proceed with using test_hero_data as needed
            else:
                logger.warning(f"Hero '{hero_name}' not found in hero data.")
                return  # Exit the task if the hero is not found

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
                            "text": "Please analyze this image and generate a JSON object containing values for 'height', 'weight', 'age', 'species', 'role', and 'element' from the 'Hero Information' section in the screenshot. Respond with only valid JSON using the mentioned keys, and ignore any icons or other irrelevant information. If the existing data for a particular key is more complete than what you find, use the pre-existing value in your JSON response. Current data for this hero: " + json.dumps(db_hero_data['heroInformation']['bioFields']), 
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
            response_json = response.json()            

            # Check for errors
            if 'error' in response_json:
                logger.error(f"OpenAI API error: {response_json['error']}")
                return

            # Access the 'choices' data
            extracted_data = response_json['choices'][0]['message']['content']
            cleaned_data = extracted_data.strip('```json').strip('```')

            # Log the response JSON
            logger.info(cleaned_data)

            # Attempt to parse the extracted data as JSON
            try:                
                hero_data = json.loads(cleaned_data)
                logger.info("Successfully processed JSON from AI response")
                
                # POST the hero's story and ID to the specified URL
                update_url = current_app.config['WORDPRESS_SITE'] + '/wp-json/heavenhold/v1/update-bio'
                payload = {
                    'hero_id': db_hero_data['databaseId'],
                    'age': hero_data['age'],
                    'height': hero_data['height'],
                    'weight': hero_data['weight'],
                    'species': hero_data['species'],
                    'role': hero_data['role'],
                    'element': hero_data['element'],
                }
                response = requests.post(update_url, json=payload)
                response.raise_for_status()
                logger.info("Hero bio updated successfully")
                
                # Delete the image after processing (if desired)
                s3_client.delete_object(Bucket=bucket_name, Key=key)
                fetch_hero_data.delay()
                logger.info(f"{key} processed successfully, deleting from S3 bucket.")
            except json.JSONDecodeError as e:
                logger.error("Failed to parse JSON from AI response")
                logger.error(e)
                # Handle the error accordingly
        else:
            logger.warning("Hero data not found in cache")

    except Exception as e:
        logger.exception(f"Error processing image {key} from folder '{folder}':")

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
            logger.info(f"Found {len(response['Contents'])} items in S3 bucket folder '{folder}'.")

            for obj in response['Contents']:
                key = obj['Key']
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
                    else:
                        process_image_task.delay(key, folder)
                else:
                    logger.warning(f"Invalid filename format: {filename}. Skipping processing.")
        else:
            logger.info(f"No images found in the S3 bucket folder '{folder}'.")
    except Exception as e:
        logger.exception(f"Error checking S3 bucket folder '{folder}':")

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