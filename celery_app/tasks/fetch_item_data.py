import logging
import json
import requests
from celery import shared_task
from ..graphql.item_query import item_query
from ..utils import redis_client
from config import WORDPRESS_SITE, WORDPRESS_USERNAME, WORDPRESS_PASSWORD

logger = logging.getLogger(__name__)

@shared_task
def fetch_item_data():
    logger.info("Fetching item data from WordPress")
    try:
        # Prepare the GraphQL query
        query = item_query

        # Prepare the request
        url = WORDPRESS_SITE + '/graphql'
        auth = (
            WORDPRESS_USERNAME,
            WORDPRESS_PASSWORD,
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
