import asyncio
import logging
import sys
import io
import os
from typing import Literal
import uuid
import discord
import boto3
import base64
import requests
from discord.ext import commands, tasks
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

from config import DISCORD_TOKEN, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, AWS_S3_BUCKET, GUILD_ID, WORDPRESS_SITE, DISCORD_CHANNEL_ID

intents = discord.Intents(members=True, guilds=True)
intents.message_content = True

# Initialize Redis client
redis_client = redis.Redis(host='redis-service', port=6379, db=0)

dropdown_options = []
hero_name_mapping = {}

def decode_base64_to_image(base64_string):
    return io.BytesIO(base64.b64decode(base64_string))

def fetch_hero_data():
    global dropdown_options, hero_name_mapping
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
    
    return dropdown_options, hero_name_mapping

dropdown_options, hero_name_mapping = fetch_hero_data()

class Lahn(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
    
    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)        
        # Sync commands for the specific guild
        self.tree.add_command(submit_hero_illustration)
        self.tree.add_command(submit_hero_bio)
        self.tree.add_command(submit_hero_portrait)
        self.tree.add_command(submit_hero_story)
        self.tree.add_command(submit_hero_stats)
        self.tree.add_command(add_new_hero)
        await self.tree.sync(guild=guild)
        logger.info("Bot started successfully.")
        

bot = Lahn()

@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user}')
    check_redis_for_messages.start()

@tasks.loop(seconds=10)
async def check_redis_for_messages():
    try:
        message = redis_client.lpop('discord_message_queue')  # Fetch message from Redis queue
        if message:
            message_data = json.loads(message)
            channel_id = int(message_data['channel_id'])                        
            
            # Check if it's an embed
            if message_data.get('is_embed', False):
                embed_data = message_data['embed']
                task_id = message_data['task_id']  # Task ID for tracking poll result
                await send_embed_to_channel(channel_id, embed_data, task_id)
            else:
                content = message_data['message']
                await send_message_to_channel(channel_id, content)
    except Exception as e:
        logger.error(f"Error while checking Redis: {e}")

async def send_message_to_channel(channel_id: int, message: str):
    channel = bot.get_channel(channel_id)
    if channel:
        await channel.send(message)
        logger.info(f"Message sent to channel {channel_id}: {message}")
    else:
        logger.error(f"Channel {channel_id} not found")

async def send_embed_to_channel(channel_id: int, embed_data: dict, task_id: str, image: str = None, filename: str = None):
    channel = bot.get_channel(channel_id)
    if channel:
        # **Ensure clearing of the message** after processing
        redis_client.delete(f"discord_message_queue:{task_id}")

        embed = discord.Embed.from_dict(embed_data)  # Recreate the embed from the dictionary
        poll_message = await channel.send(embed=embed)
        logger.info(f"Embed sent to channel {channel_id}, waiting for votes.")
        
        # Add reactions for voting
        await poll_message.add_reaction('✅')
        await poll_message.add_reaction('❌')
        
        # Define a check for valid reactions
        def check(reaction, user):
            return user != bot.user and str(reaction.emoji) in ['✅', '❌'] and reaction.message.id == poll_message.id

        # Loop until a reaction is received or the time runs out
        upvotes = 0
        downvotes = 0
        try:
            while True:
                reaction, user = await bot.wait_for('reaction_add', timeout=5, check=check)  # Timeout set to 5 seconds to check regularly
                # Fetch the message to get the updated reactions
                poll_message = await channel.fetch_message(poll_message.id)

                # Tally the votes after every reaction
                for react in poll_message.reactions:
                    if react.emoji == '✅':
                        upvotes = react.count - 1  # Subtract 1 to exclude the bot's reaction
                    elif react.emoji == '❌':
                        downvotes = react.count - 1  # Subtract 1 to exclude the bot's reaction

                # If any reaction received, break the loop immediately
                if upvotes > 0 or downvotes > 0:
                    break

        except asyncio.TimeoutError:
            logger.info(f"No reactions received in time for task {task_id}. Proceeding with final counts.")

        logger.info(f"Poll result for task {task_id}: ✅ {upvotes}, ❌ {downvotes}")

        # Store the result in Redis (for the Celery task to read)
        poll_result = {
            'upvotes': upvotes,
            'downvotes': downvotes
        }
        redis_client.set(f"discord_poll_result:{task_id}", json.dumps(poll_result))
        redis_client.expire(f"discord_poll_result:{task_id}", 60) 

        # Update the embed based on the poll results
        if upvotes > downvotes:
            embed.color = discord.Color.green()  # Success
            embed.set_footer(text="Thanks for confirming! I'll update the site now.")
        elif downvotes > upvotes:
            embed.color = discord.Color.red()  # Failure
            embed.set_footer(text="Okay, I won't update the site then.")
        elif upvotes == 0 and downvotes == 0:
            embed.color = discord.Color.orange()  # No confirmation
            embed.set_footer(text="Confirmation not received, I'll create a revision.")

        if image and filename:
            # Add the image to the embed
            image_bytes = decode_base64_to_image(image)
            discord_file = discord.File(image_bytes, filename=filename)
            embed.set_image(url=f"attachment://{filename}")
            await poll_message.edit(embed=embed, file=discord_file)
        else:            
            await poll_message.edit(embed=embed)
    else:
        logger.error(f"Channel {channel_id} not found")

@bot.command(name="manual_sync_commands", hidden=True)
@commands.is_owner()
async def manual_sync_commands(ctx):
    try:
        logger.info("Syncing commands...")         
        ctx.bot.tree.remove_command(submit_hero_story)
        ctx.bot.tree.remove_command(submit_hero_portrait)
        ctx.bot.tree.remove_command(submit_hero_illustration)
        ctx.bot.tree.remove_command(submit_hero_bio)
        ctx.bot.tree.remove_command(submit_hero_stats)
        ctx.bot.tree.remove_command(add_new_hero)
        ctx.bot.tree.add_command(add_new_hero)
        ctx.bot.tree.add_command(submit_hero_stats)
        ctx.bot.tree.add_command(submit_hero_illustration)
        ctx.bot.tree.add_command(submit_hero_bio)
        ctx.bot.tree.add_command(submit_hero_portrait)
        ctx.bot.tree.add_command(submit_hero_story)
        await ctx.bot.tree.sync()
        await ctx.send("Commands synced successfully.")
        logger.info("Commands synced successfully!")
    except Exception as e:
        logger.error(f"Error syncing commands: {e}")
        await ctx.send("Error syncing commands.")

# Define the slash command
@app_commands.command(name="submit_hero_story", description="Upload an image with a hero's story to update the site.")
@app_commands.describe(hero="Select a hero", image="Attach an image")
async def submit_hero_story(interaction: discord.Interaction, hero: str, image: discord.Attachment):
    dropdown_options, hero_name_mapping = fetch_hero_data()
    if dropdown_options is None:
        logger.info("No hero data found, please try again.")
        dropdown_options, hero_name_mapping = fetch_hero_data()
        await interaction.followup.send("No hero data found. Please try again later.")
        return
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

        # Prepare the embed
        embed = discord.Embed(
            title="Hero Story Uploaded",
            description=f"**Hero:** {hero_title}\nStory image uploaded successfully!"
        )

        embed.color = discord.Color.green()

        # Attach the image directly to the Discord message and add it to the embed
        discord_file = discord.File(io.BytesIO(file_content), filename=filename)
        embed.set_image(url=f"attachment://{filename}")

        # Send the embed with the attached image
        await interaction.followup.send(embed=embed, file=discord_file)

# Autocomplete function for hero
@submit_hero_story.autocomplete('hero')
async def story_hero_name_autocomplete(interaction: discord.Interaction, current: str):
    global dropdown_options
    if dropdown_options is None:
        logger.info("No hero data found, trying to reload...")
        dropdown_options, hero_name_mapping = fetch_hero_data()
        if dropdown_options is None: 
            await interaction.followup.send("There was a problem getting the list of heroes. Please try again later.")
            return
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

        # Send a confirmation message with the image
        embed = discord.Embed(
            title="Hero Portrait Uploaded",
            description=f"**Hero:** {hero_title}\nPortrait image uploaded successfully!"
        )

        embed.color = discord.Color.green()

        # Attach the image directly to the Discord message and add it to the embed
        discord_file = discord.File(io.BytesIO(file_content), filename=filename)
        embed.set_image(url=f"attachment://{filename}")

        # Send the embed with the attached image
        await interaction.followup.send(embed=embed, file=discord_file)

# Autocomplete function for hero
@submit_hero_portrait.autocomplete('hero')
async def portrait_hero_name_autocomplete(interaction: discord.Interaction, current: str):
    global dropdown_options
    if dropdown_options is None:
        logger.info("No hero data found, please try again.")
        dropdown_options, hero_name_mapping = fetch_hero_data()
        if dropdown_options is None: 
            await interaction.followup.send("No hero data found. Please try again later.")
            return
    # Suggest hero names based on user input
    suggestions = []
    for slug, title in dropdown_options:
        if current.lower() in title.lower():
            suggestions.append(app_commands.Choice(name=title, value=slug))
            if len(suggestions) >= 25:
                break
    return suggestions

# Define the slash command
@app_commands.command(name="submit_hero_bio", description="Upload an image with a hero's bio to update the site.")
@app_commands.describe(hero="Select a hero", image="Attach an image")
async def submit_hero_bio(interaction: discord.Interaction, hero: str, image: discord.Attachment):
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
            Key=f"hero-bios/{new_filename}", 
            Body=file_content
        )
        logger.info(f"Uploaded image to S3: {new_filename}")

        # Prepare the embed
        embed = discord.Embed(
            title="Hero Bio Uploaded",
            description=f"**Hero:** {hero_title}\nBio image uploaded successfully!"
        )

        embed.color = discord.Color.green()

        # Attach the image directly to the Discord message and add it to the embed
        discord_file = discord.File(io.BytesIO(file_content), filename=filename)
        embed.set_image(url=f"attachment://{filename}")

        # Send the embed with the attached image
        await interaction.followup.send(embed=embed, file=discord_file)

# Autocomplete function for hero
@submit_hero_bio.autocomplete('hero')
async def bio_hero_name_autocomplete(interaction: discord.Interaction, current: str):
    global dropdown_options
    if dropdown_options is None:
        logger.info("No hero data found, please try again.")
        dropdown_options, hero_name_mapping = fetch_hero_data()
        if dropdown_options is None: 
            await interaction.followup.send("No hero data found. Please try again later.")
            return
    # Suggest hero names based on user input
    suggestions = []
    for slug, title in dropdown_options:
        if current.lower() in title.lower():
            suggestions.append(app_commands.Choice(name=title, value=slug))
            if len(suggestions) >= 25:
                break
    return suggestions

@app_commands.command(name="submit_hero_stats", description="Upload an image with a hero's level 100 stats to update the site.")
@app_commands.describe(hero="Select a hero", image="Attach an image")
async def submit_hero_stats(interaction: discord.Interaction, hero: str, image: discord.Attachment):
    # Get the hero title from the slug
    hero_title = hero_name_mapping.get(hero, "Unknown Hero")
    # Acknowledge the interaction
    await interaction.response.defer(thinking=True)
    
    if image is not None:
        filename = image.filename
        file_content = await image.read()

        # Generate a GUID
        guid = str(uuid.uuid4())

        # Construct the new filename
        file_extension = os.path.splitext(filename)[1]
        new_filename = f"{hero}_{guid}{file_extension}"

        # Upload the image to S3 (optional, if you still want it for storage)
        s3_client = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION,
        )
        s3_client.put_object(
            Bucket=AWS_S3_BUCKET, 
            Key=f"hero-stats/{new_filename}", 
            Body=file_content
        )
        logger.info(f"Uploaded image to S3: {new_filename}")

        # Prepare the embed
        embed = discord.Embed(
            title="Hero Stats Uploaded",
            description=f"**Hero:** {hero_title}\nStats image uploaded successfully!"
        )

        embed.color = discord.Color.green()

        # Attach the image directly to the Discord message and add it to the embed
        discord_file = discord.File(io.BytesIO(file_content), filename=filename)
        embed.set_image(url=f"attachment://{filename}")

        # Send the embed with the attached image
        await interaction.followup.send(embed=embed, file=discord_file)

# Autocomplete function for hero
@submit_hero_stats.autocomplete('hero')
async def stats_hero_name_autocomplete(interaction: discord.Interaction, current: str):
    global dropdown_options
    if dropdown_options is None:
        logger.info("No hero data found, please try again.")
        dropdown_options, hero_name_mapping = fetch_hero_data()
        if dropdown_options is None: 
            await interaction.followup.send("No hero data found. Please try again later.")
            return
    # Suggest hero names based on user input
    suggestions = []
    for slug, title in dropdown_options:
        if current.lower() in title.lower():
            suggestions.append(app_commands.Choice(name=title, value=slug))
            if len(suggestions) >= 25:
                break
    return suggestions

# Define the slash command
@app_commands.command(name="submit_hero_illustration", description="Upload an image with a hero's illustration (no background) to update the site.")
@app_commands.describe(hero="Select a hero", image="Attach an image")
async def submit_hero_illustration(interaction: discord.Interaction, hero: str, image: discord.Attachment, region: Literal['Global', 'Japan']):
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
            Key=f"hero-illustrations/{new_filename}", 
            Body=file_content
        )
        logger.info(f"Uploaded image to S3: {new_filename}")

        # Prepare the embed
        embed = discord.Embed(
            title="Hero Illustration Uploaded",
            description=f"**Hero:** {hero_title}\nIllustration image uploaded successfully!"
        )

        embed.color = discord.Color.green()

        # Attach the image directly to the Discord message and add it to the embed
        discord_file = discord.File(io.BytesIO(file_content), filename=filename)
        embed.set_image(url=f"attachment://{filename}")

        # Send the embed with the attached image
        await interaction.followup.send(embed=embed, file=discord_file)

# Autocomplete function for hero
@submit_hero_illustration.autocomplete('hero')
async def illustration_hero_name_autocomplete(interaction: discord.Interaction, current: str):
    global dropdown_options
    if dropdown_options is None:
        logger.info("No hero data found, please try again.")
        dropdown_options, hero_name_mapping = fetch_hero_data()
        if dropdown_options is None: 
            await interaction.followup.send("No hero data found. Please try again later.")
            return
    # Suggest hero names based on user input
    suggestions = []
    for slug, title in dropdown_options:
        if current.lower() in title.lower():
            suggestions.append(app_commands.Choice(name=title, value=slug))
            if len(suggestions) >= 25:
                break
    return suggestions

# Define the slash command
@app_commands.command(name="add_new_hero", description="Add a new blank hero to the site.")
@app_commands.describe(name="Enter the hero's full name (with title)")
async def add_new_hero(interaction: discord.Interaction, name: str):
    # Get the hero title from the slug
    hero_title = hero_name_mapping.get(name, "Unknown Hero")
    # Acknowledge the interaction
    await interaction.response.defer(thinking=True)
    # Process the image and hero name as needed
    if hero_title == "Unknown Hero":
        payload = {
            'hero_title': name,
        }
        update_url = WORDPRESS_SITE + '/wp-json/heavenhold/v1/add-new-hero'
        response = requests.post(update_url, data=payload)
        # Raise an exception if the response contains an error
        response.raise_for_status()
        # Send a confirmation message
        await interaction.followup.send(f"**Hero:** {name} created! Hero lists will update in 2-3 minutes.")
    else:
        await interaction.followup.send(f"**Hero:** {name}\nHero already exists.")

bot.run(DISCORD_TOKEN)