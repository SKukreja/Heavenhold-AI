stat_prompt = '''
Please analyze this image and generate a JSON object containing numerical values for the line items you see under the 'Hero Information' section in the screenshot, and the line items you see under 'Co-op Expedition'.

The possible values you will see under 'Hero Information' are:
'Atk', 'Def', 'HP', 'Crit', 'Heal', 'Damage Reduction', 'Earth Resistance', 'Fire Resistance', 'Water Resistance', 'Basic Resistance', 'Dark Resistance', 'Light Resistance'. If any are not present, just put 0 for that value.

These values and the line items you will see under 'Co-op Expedition' should be combined together into your JSON response. 'Co-op Expedition' lines can be listed under the 'passives' key as an array of boolean, string, and numerical values. 

Your response should be structured as shown below:

{
    "atk": int,
    "def": int,
    "hp": int,
    "crit": int,
    "heal": int,
    "damage_reduction": int,
    "basic_resistance": int,
    "light_resistance": int,
    "dark_resistance": int,
    "fire_resistance": int,
    "earth_resistance": int,
    "water_resistance": int,
    "compatible_equipment": [
        string,
        string,
    ],
    "passives": [
        {                                             
            "affects_party": bool,
            "stat": string (limited to options mentioned below),
            "value": int
        },
        {                                             
            "affects_party": bool,
            "stat": string (limited to options mentioned below),
            "value": int
        },
    ]
}

The possible values you will see next to 'Compatible Equipment' are:
'One-Handed Sword', 'Two-Handed Sword', 'Bow', 'Rifle', 'Staff', 'Basket', 'Gauntlet', 'Claw', 'Shield', 'Accessory', 'Merch', 'Relic', and 'Cards'. If any are not present, just put an empty array for that value.

Possible values for 'stat' are below, select the best one based on what you see in the image:

Atk
Crit Hit Chance
Damage Reduction
Def
HP
Melee Damage
Range Damage
Skill Damage
Atk increase on enemy kill
HP recovery on enemy kill
Seconds of weapon skill Regen time on enemy kill
Shield increase on battle start
Shield increase on enemy kill
Weapon Skill Regen Speed
Fire Atk
Earth Atk
Water Atk
Dark Atk
Light Atk
Basic Atk
Heal (Flat)
Heal (%)
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

Respond with only valid JSON using the mentioned structure, and ignore any icons or other irrelevant information. 
'''