import logging
import json
import requests
from celery import shared_task
from ..graphql.hero_query import hero_query
from ..utils import redis_client
from config import WORDPRESS_SITE, WORDPRESS_USERNAME, WORDPRESS_PASSWORD

logger = logging.getLogger(__name__)

@shared_task
def fetch_hero_data():
    logger.info("Fetching hero data from WordPress")
    try:
        # Base URL and authentication
        url = WORDPRESS_SITE + '/graphql'
        auth = (WORDPRESS_USERNAME, WORDPRESS_PASSWORD)
        headers = {'Content-Type': 'application/json'}

        # Initialize variables for pagination
        all_results = []
        has_next_page = True
        after_cursor = None

        while has_next_page:
            # Prepare the query and variables
            query = hero_query
            variables = {'after': after_cursor}
            data = {'query': query, 'variables': variables}

            # Make the request
            response = requests.post(url, json=data, headers=headers, auth=auth)
            response.raise_for_status()

            # Parse the response
            result = response.json()

            # Check if 'data' and 'heroes' are in the result
            if 'data' in result and 'heroes' in result['data']:
                heroes_data = result['data']['heroes']
                all_results.extend(heroes_data.get('nodes', []))

                # Check if 'pageInfo' exists
                page_info = heroes_data.get('pageInfo')
                if page_info:
                    after_cursor = page_info.get('endCursor')
                    has_next_page = page_info.get('hasNextPage', False)
                else:
                    logger.warning("No pageInfo found in the response. Stopping pagination.")
                    break
            else:
                # Log the error details
                logger.error(f"Unexpected response structure: {json.dumps(result)}")
                break

            logger.info(f"Fetched {len(heroes_data.get('nodes', []))} heroes; Total so far: {len(all_results)}")

        # Store the combined result in Redis
        redis_client.set('hero_data', json.dumps(all_results))

        logger.info(f"Hero data cached successfully with {len(all_results)} total heroes.")

    except Exception as e:
        logger.exception("Error fetching hero data:")
