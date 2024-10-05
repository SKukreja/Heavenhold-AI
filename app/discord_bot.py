import logging
import sys
import os
from typing import Literal
import uuid
import discord
import boto3
from discord.ext import commands
from discord import app_commands
import redis
import json

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s: %(levelname)s/%(processName)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

# Add parent directory to sys.path
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import DISCORD_TOKEN, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, AWS_S3_BUCKET, GUILD_ID

intents = discord.Intents.default()
intents.message_content = True

# Initialize Redis client
redis_client = redis.Redis(host='redis-service', port=6379, db=0)

# Fetch hero data
cached_data = redis_client.get('hero_data')
if cached_data is not None:
    logger.info("Retrieved hero data from cache.")
    hero_data = json.loads(cached_data)
    heroes_list = hero_data['data']['heroes']['nodes']
    # Create a list of (slug, title) tuples
    dropdown_options = sorted([(hero['slug'], hero['title']) for hero in heroes_list], key=lambda x: x[1])
    # Create a mapping for easy lookup
    hero_name_mapping = {hero['slug']: hero['title'] for hero in heroes_list}
else:
    dropdown_options = []
    hero_name_mapping = {}
    logger.info("No hero data found in Redis.")

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
    
    async def setup_hook(self):
        # guild = discord.Object(id=GUILD_ID)        
        # Sync commands for the specific guild
        # await self.tree.sync(guild=guild)
        logger.info("Bot started successfully.")
        

bot = MyBot()

@bot.event
async def on_ready():
    logger.info(f'We have logged in as {bot.user}')

@bot.command(name="manual_sync_commands", hidden=True)
@commands.is_owner()
async def manual_sync_commands(ctx):
    try:
        logger.info("Syncing commands...") 
        guild = discord.Object(id=GUILD_ID)
        ctx.bot.tree.remove_command(submit_hero_story)
        ctx.bot.tree.remove_command(submit_hero_portrait)
        ctx.bot.tree.add_command(submit_hero_portrait)
        ctx.bot.tree.add_command(submit_hero_story)
        await ctx.bot.tree.sync(guild=guild)
        await ctx.send("Commands synced successfully!")
        logger.info("Commands synced successfully.")
    except Exception as e:
        logger.error(f"Error syncing commands: {e}")
        await ctx.send("Error syncing commands.")

# Define the slash command
@app_commands.command(name="submit_hero_story", description="Upload an image with a hero's story to update the site.")
@app_commands.describe(hero="Select a hero", image="Attach an image")
async def submit_hero_story(interaction: discord.Interaction, hero: str, image: discord.Attachment):
    # Get the hero title from the slug
    hero_title = hero_name_mapping.get(hero, "Unknown Hero")
    # Acknowledge the interaction
    await interaction.response.defer(thinking=True)
    # Process the image and hero name as needed
    if image is not None:
        filename = image.filename
        file_content = await image.read()

        # Generate a GUID
        guid = str(uuid.uuid4())

        # Construct the new filename
        file_extension = os.path.splitext(filename)[1]
        new_filename = f"{hero}_{guid}{file_extension}"

        # Upload the image to S3
        s3_client = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION,
        )
        s3_client.put_object(
            Bucket=AWS_S3_BUCKET, 
            Key=f"hero-stories/{new_filename}", 
            Body=file_content
        )
        logger.info(f"Uploaded image to S3: {new_filename}")

        # Send a confirmation message
        await interaction.followup.send(f"**Hero:** {hero_title}\nStory image uploaded successfully! Revision will be added to queue in 2-3 minutes.")

# Autocomplete function for hero
@submit_hero_story.autocomplete('hero')
async def hero_name_autocomplete(interaction: discord.Interaction, current: str):
    # Suggest hero names based on user input
    suggestions = []
    for slug, title in dropdown_options:
        if current.lower() in title.lower():
            suggestions.append(app_commands.Choice(name=title, value=slug))
            if len(suggestions) >= 25:
                break
    return suggestions

# Define the slash command
@app_commands.command(name="submit_hero_portrait", description="Upload an image with a hero's portrait to update the site.")
@app_commands.describe(hero="Select a hero", image="Attach an image", region="Select a region")
async def submit_hero_portrait(interaction: discord.Interaction, hero: str, image: discord.Attachment, region: Literal['Global', 'Japan']):
    # Get the hero title from the slug
    hero_title = hero_name_mapping.get(hero, "Unknown Hero")
    # Acknowledge the interaction
    await interaction.response.defer(thinking=True)
    # Process the image and hero name as needed
    if image is not None:
        filename = image.filename
        file_content = await image.read()

        # Generate a GUID
        guid = str(uuid.uuid4())

        # Construct the new filename
        file_extension = os.path.splitext(filename)[1]
        new_filename = f"{hero}_{region}_{guid}{file_extension}"

        # Upload the image to S3
        s3_client = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION,
        )
        s3_client.put_object(
            Bucket=AWS_S3_BUCKET, 
            Key=f"hero-portraits/{new_filename}", 
            Body=file_content
        )
        logger.info(f"Uploaded image to S3: {new_filename}")

        # Send a confirmation message
        await interaction.followup.send(f"**Hero:** {hero_title}\n**Region:** {region}\nPortrait image uploaded successfully! Revision will be added to queue in 2-3 minutes.")

# Autocomplete function for hero
@submit_hero_portrait.autocomplete('hero')
async def hero_name_autocomplete(interaction: discord.Interaction, current: str):
    # Suggest hero names based on user input
    suggestions = []
    for slug, title in dropdown_options:
        if current.lower() in title.lower():
            suggestions.append(app_commands.Choice(name=title, value=slug))
            if len(suggestions) >= 25:
                break
    return suggestions

bot.run(DISCORD_TOKEN)

