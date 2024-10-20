import redis
import logging
import base64
import requests
from PIL import Image
import numpy as np
from config import AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION

logger = logging.getLogger(__name__)

redis_client = redis.Redis(host='redis-service', port=6379, db=0)

# Store boto3 config as a variable to re-use
boto3_config = {
    'aws_access_key_id': AWS_ACCESS_KEY_ID,
    'aws_secret_access_key': AWS_SECRET_ACCESS_KEY,
    'region_name': AWS_REGION
}

def format_option(option):
    """Format each option for display."""
    if option["is_range"]:
        return f"{option['stat']} {option['minimum_value']} - {option['maximum_value']}"
    else:
        return f"{option['stat']} {option['value']}"
    
def format_engraving(option):
    """Format each option for display."""
    return f"{option['stat']} {option['value']}"

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