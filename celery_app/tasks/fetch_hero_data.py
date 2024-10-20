import logging
import json
import requests
from ..graphql.hero_query import hero_query
from celery import shared_task
from ..utils import redis_client
from config import WORDPRESS_SITE, WORDPRESS_USERNAME, WORDPRESS_PASSWORD

logger = logging.getLogger(__name__)

@shared_task
def fetch_hero_data():
    logger.info("Fetching hero data from WordPress")
    try:
        # Prepare the GraphQL query
        query = hero_query

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
        redis_client.set('hero_data', json.dumps(result))

        logger.info("Hero data cached successfully")

        # Log a summary of the data
        hero_count = len(result['data']['heroes']['nodes'])
        logger.info(f"Cached data contains {hero_count} heroes.")
    except Exception as e:
        logger.exception("Error fetching hero data:")