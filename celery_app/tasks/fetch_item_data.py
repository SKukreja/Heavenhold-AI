import logging
import json
import requests
from celery import shared_task
from ..utils import redis_client
from ..graphql.item_query import item_query
from config import WORDPRESS_SITE, WORDPRESS_USERNAME, WORDPRESS_PASSWORD

logger = logging.getLogger(__name__)

@shared_task
def fetch_item_data():
    logger.info("Fetching item data from WordPress")
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
            # GraphQL query with pagination support
            query = item_query
            variables = {'after': after_cursor}
            data = {'query': query, 'variables': variables}

            # Make the request
            response = requests.post(url, json=data, headers=headers, auth=auth)
            response.raise_for_status()

            # Parse the response
            result = response.json()

            # Check if 'data' and 'items' are in the result
            if 'data' in result and 'items' in result['data']:
                items_data = result['data']['items']
                all_results.extend(items_data.get('nodes', []))

                # Check if 'pageInfo' exists
                page_info = items_data.get('pageInfo')
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

            logger.info(f"Fetched {len(items_data.get('nodes', []))} items; Total so far: {len(all_results)}")

        # Store the combined result in Redis
        redis_client.set('item_data', json.dumps(all_results))

        logger.info(f"Item data cached successfully with {len(all_results)} total items.")

    except Exception as e:
        logger.exception("Error fetching item data:")
