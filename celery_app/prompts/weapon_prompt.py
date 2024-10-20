weapon_prompt = '''
Please analyze this image and generate a JSON object containing updated values for the line items you see in the screenshot based on the field descriptions below.
You will not receive all information about this item in one screenshot, so you will need to piece together the full object from multiple screenshots. I will provide you with the information we have so far at the end of this brief.

For any arrays such as 'main_option', 'sub_option', or 'engraving_options', you should append or prepend new lines to the array based on the order they appear in the image.                

Your response should be structured as outlined below:

{
    "name": string, // The title of the weapon displayed at the top of the image next to the icon
    "rarity": string, // The rarity of the weapon displayed at the top of the image, under the name either Epic, Legend, Unique, or Rare
    "weapon_type": string, // The weapon type displayed in the image under the title, next to the rarity (Epic, Legend, Unique, Rare)
    "exclusive": bool, // True if the word 'only' is present in the image, False otherwise
    "hero": string, // The name of the hero this weapon is exclusive to, displayed next to 'only', or an empty string if the weapon is not exclusive. Always include the full name of the hero, which usually includes a title preceding the first name of the hero.
    "exclusive_effects": string, // Any lines of text displayed under a ('[' + hero + ' only]') headline combined with a line break to separate them, or an empty string if the weapon is not exclusive. Do not include the bracket headlines in the string.
    "min_dps": int, // The first number in the range displayed next to 'DPS'
    "max_dps": int, // The second number in the range displayed next to 'DPS'
    "weapon_skill_name": string, // The name of the weapon skill displayed on the left side of the image, always ends with Lv.# where # is the level of the skill and should be included in the string
    "weapon_skill_atk": int, // The green number displayed underneath the weapon skill name, in the form "Atk: #% DPS"
    "weapon_skill_regen_time": int, // The number of seconds displayed next to "Regen time:"
    "weapon_skill_description": string, // The description of the weapon skill displayed on the left side of the image under the Regen time
    "weapon_skill_chain": string, // Either "Injured", "Downed", or "Airborne" depending on which one is present in the weapon skill description    
    "main_option": [ // An array of stats and numbers displayed under the headline "Main Option", or above the Sub Option header if "Main Option" is not present. It's any line below the Lv.1, Lv. 95, Lv. 100 and Engrave tabs that does not have a headline in square brackets above it. Always be sure to combine with the existing values if there are any, updating values when the chosen 'stat' is already present in the array.
        {                                             
            "stat": string, // (limited to options mentioned below)
            "is_range": bool, // True if the stat is a range, False otherwise
            "value": int, // The number displayed next to the stat if it is not a range, or 0 if it is a range
            "minimum_value": int, // The first number in the range if 'is_range' is True, or 0 if 'is_range' is False
            "maximum_value": int // The second number in the range if 'is_range' is True, or 0 if 'is_range' is False
        },
        {                                             
            "stat": string, // (limited to options mentioned below)
            "is_range": bool, // True if the stat is a range, False otherwise
            "value": int, // The number displayed next to the stat if it is not a range, or 0 if it is a range
            "minimum_value": int, // The first number in the range if 'is_range' is True, or 0 if 'is_range' is False
            "maximum_value": int // The second number in the range if 'is_range' is True, or 0 if 'is_range' is False
        },
    ],
    "max_lines": int, // If the headline "Sub Option" is present, you will see this number in the format "0 to x abilities granted", where x is the number you should put here. If "Sub Option" is not present, use the pre-existing value or 0 if there isn't one.
    "sub_option": [ // An array of stats and numbers displayed under the headline "Sub Option" if present. Always be sure to combine with the existing values if there are any, updating values when the chosen 'stat' is already present in the array.
        {                                             
            "stat": string, // (limited to options mentioned below)
            "is_range": bool, // True if the stat is a range, False otherwise
            "value": int, // The number displayed next to the stat if it is not a range, or 0 if it is a range
            "minimum_value": int, // The first number in the range if 'is_range' is True, or 0 if 'is_range' is False
            "maximum_value": int // The second number in the range if 'is_range' is True, or 0 if 'is_range' is False
        },
        {                                             
            "stat": string, // (limited to options mentioned below)
            "is_range": bool, // True if the stat is a range, False otherwise
            "value": int, // The number displayed next to the stat if it is not a range, or 0 if it is a range
            "minimum_value": int, // The first number in the range if 'is_range' is True, or 0 if 'is_range' is False
            "maximum_value": int // The second number in the range if 'is_range' is True, or 0 if 'is_range' is False
        },
    ],
    "limit_break_5_option": string, // (limited to 'stat' options below, do not shorten)
    "limit_break_5_value": int, // The number displayed under the headline "[Required Limit Break 5]" if present, or 0 if "Limit Break 5" is not present
    "engraving_options": [ // An array of stats and numbers displayed under the headline "Exclusive Engraving Option", or an empty array if "Exclusive Engraving Option" is not present
        {                                             
            "stat": string, // (limited to 'stat' options below)
            "value": int
        },
        {                                             
            "stat": string, // (limited to 'stat' options below)
            "value": int
        },
    ]
}

The possible values you will see for 'weapon_type' are:
'One-Handed Sword', 'Two-Handed Sword', 'Bow', 'Rifle', 'Staff', 'Basket', 'Gauntlet', 'Claw', 'Shield', 'Accessory', 'Merch', 'Relic', and 'Cards'. If any are not present, just put an empty array for that value.

Possible values for 'stat' are below, select the best one based on what you see in the image and return it exactly as shown below:

Fire Atk
Earth Atk
Water Atk
Dark Atk
Light Atk
Basic Atk
Fire type Atk (%)
Earth type Atk (%)
Water type Atk (%)
Dark type Atk (%)
Light type Atk (%)
Basic type Atk (%)
Atk (%)
Crit Hit Chance
Damage Reduction
Def (Flat)
Def (%)
Heal (Flat)
Heal (%)
HP (Flat)
HP (%)
Atk increase on enemy kill
HP recovery on enemy kill
Seconds of weapon skill Regen time on enemy kill
Shield increase on battle start
Shield increase on enemy kill
Melee Damage
Range Damage
Skill Damage
Weapon Skill Regen Speed
Atk, Heal [] for injured Chain Skills
Atk Decrease % negated
Def Decrease % negated
Doom Damage % negated
Injury Damage % negated
On hit extra damage
On hit heal allies
Increase damage to enemies with HP
Decrease damage taken by % of increased Skill Damage
Increase damage to tanker Hero
Crit Hit Multiplier
When a shield is present, damage dealt increases by x% while damage taken decreases by x%

Please note that the (Flat) or (%) version of each stat should be chosen accordingly based on whether the number ends with a percentage or is a flat integer/float.
Respond with only valid JSON using the mentioned structure, and ignore any icons or other irrelevant information. 
Remember, if you don't see any information in the screenshot for a particular field such as main_option, engraving_options or sub_option, use the existing information under the corresponding field below, or add any new lines if you see the what could be the end or beginning of the pre-recorded field values below where another screenshot may have cut off.
Here is the current data we have so far for this item:
'''