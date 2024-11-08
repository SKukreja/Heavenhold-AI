# app/celery_tasks.py

import logging
import boto3
import threading
from celery import Celery
from .tasks.fetch_hero_data import fetch_hero_data
from .tasks.fetch_item_data import fetch_item_data
from .tasks.process_hero_story import process_hero_story_task
from .tasks.process_hero_portrait import process_hero_portrait_task
from .tasks.process_hero_illustration import process_hero_illustration_task
from .tasks.process_hero_bio import process_hero_bio_task
from .tasks.process_hero_stats import process_hero_stats_task
from .tasks.process_weapon_information import process_weapon_information_task
from .utils import handle_expired_keys, redis_client, boto3_config
from config import DEV_BROKER_URL, DEV_RESULT_BACKEND, AWS_S3_BUCKET

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s: %(levelname)s/%(processName)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

logger = logging.getLogger(__name__)

def make_celery():
    # Initialize Celery with the broker and backend defined in the configuration
    celery = Celery(
        __name__,
        broker=DEV_BROKER_URL,
        backend=DEV_RESULT_BACKEND,
    )

    # Return the Celery instance
    return celery

# Create the Celery instance
celery = make_celery()

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

    sender.add_periodic_task(
        30.0,  # Run every 60 seconds
        check_and_process_s3_images.s('weapon-information'),
        name="Check 'weapon-information' folder in S3 bucket",
        countdown=50,  # Stagger by 40 seconds
    )

    # Schedule to fetch hero stories data every 3 minutes
    sender.add_periodic_task(
        360.0,  # Run every 3 minutes
        fetch_hero_data.s(),
        name="Fetch hero data from WordPress",
        countdown=0  # No delay needed
    )

    # Schedule to fetch hero stories data every 3 minutes
    sender.add_periodic_task(
        360.0,  # Run every 3 minutes
        fetch_item_data.s(),
        name="Fetch item data from WordPress",
        countdown=10  # No delay needed
    )

    # Trigger tasks immediately on startup
    fetch_hero_data.delay()
    fetch_item_data.delay()

@celery.task
def check_and_process_s3_images(folder):
    global redis_client, bucket_name, boto3_config
    try:
        # Initialize S3 client using app.config variables
        s3_client = boto3.client('s3', **boto3_config)

        # Check for images in the specified S3 bucket folder
        response = s3_client.list_objects_v2(
            Bucket=AWS_S3_BUCKET,
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
                    file_name_parts = filename_without_extension.split('_')
                    if len(file_name_parts) >= 2:
                        slug_name = file_name_parts[0]
                        # Call the appropriate task
                        if folder == "hero-stories":
                            process_hero_story_task.delay(key, folder, slug_name)
                        elif folder == "hero-portraits":
                            region = file_name_parts[1]
                            process_hero_portrait_task.delay(key, folder, slug_name, region)
                        elif folder == "hero-illustrations":
                            region = file_name_parts[1]
                            process_hero_illustration_task.delay(key, folder, slug_name, region)
                        elif folder == "hero-bios":                        
                            process_hero_bio_task.delay(key, folder, slug_name)
                        elif folder == "hero-stats":                        
                            process_hero_stats_task.delay(key, folder, slug_name)
                        elif folder == "weapon-information":
                            process_weapon_information_task.delay(key, folder, slug_name)
                    elif filename != '':
                        logger.warning(f"Invalid filename format: {filename}. Skipping processing.")
        else:
            logger.info(f"No images found in the S3 bucket folder '{folder}'.")
    except Exception as e:
        logger.exception(f"Error checking S3 bucket folder '{folder}': {e}")

threading.Thread(target=handle_expired_keys, daemon=True).start()