# app/celery_tasks.py

import logging
import boto3
from flask import current_app
from .app import celery
from .assistant_prompt import system_prompt
from .hero_query import hero_query
import requests
import json
import redis
import openai

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
        180.0,  # Run every 30 seconds (adjust as needed)
        check_and_process_s3_images.s('hero-stories'),
        name="Check 'hero-stories' folder in S3 bucket",
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
def process_hero_story_task(key, folder):
    test_hero = "Exorcist Swordswoman Saya"
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

            # Search for the hero with name matching test_hero
            test_hero_data = None
            for hero in heroes_list:
                if hero['title'] == test_hero:
                    test_hero_data = hero
                    break

            if test_hero_data is not None:
                # Now test_hero_data contains the data for the hero named test_hero
                logger.info(f"Found hero data for {test_hero}: {test_hero_data}")
                # Proceed with using test_hero_data as needed
            else:
                logger.warning(f"Hero '{test_hero}' not found in hero data.")
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
                            "text": "Please analyze this image and generate a JSON object containing only the updated story for this hero. The hero's story should be recorded exactly as written, but you will only receive part of it on each screenshot. Check the current data for this hero to see if part of the story has already been recorded, and then append or prepend the new parts you see in the image, piecing together as much of the full story as you can in your output. If the current story already has more than what you see, do not change it. Respond with only valid JSON data to import. Current data for this hero: " + json.dumps(test_hero_data['heroInformation']['bioFields']),
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
                "model": "gpt-4o-mini",
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
                    'hero_id': test_hero_data['databaseId'],
                    'story': hero_data['story'],
                }
                response = requests.post(update_url, json=payload)
                response.raise_for_status()
                logger.info("Hero story updated successfully")
                
                # Delete the image after processing (if desired)
                s3_client.delete_object(Bucket=bucket_name, Key=key)
                fetch_hero_data.delay()
                logger.info(f"Image {key} processed successfully.")
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
                if folder == "hero-stories":
                    process_hero_story_task.delay(key, folder)
                else:
                    process_image_task.delay(key, folder)
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

        # Simulate image processing
        import time
        time.sleep(2)

        # Delete the image after processing (if desired)
        # s3_client.delete_object(Bucket=current_app.config['AWS_S3_BUCKET'], Key=key)
        logger.info(f"Image {key} processed successfully.")
    except Exception as e:
        logger.exception(f"Error processing image {key} from folder '{folder}':")
