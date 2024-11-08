item_query = '''
query GetAllItems($after: String) {
  items(first: 100, after: $after) {
    pageInfo {
      endCursor
      hasNextPage
    }
    nodes {
      id
      databaseId
      featuredImage {
        node {
          sourceUrl
        }
      }
      title
      slug
      weapons {
        engraving {
          stat
          value
        }
        exclusive
        exclusiveEffects
        hero {
          nodes {
            id
          }
        }
        isFirstEx
        magazine
        maxDps        
        minDps
        weaponSkill
        weaponSkillAtk
        weaponSkillChain
        weaponSkillDescription
        weaponSkillName
        weaponSkillRegenTime
        weaponSkillVideo {
          node {
            sourceUrl
          }
        }
        weaponType
      }
      equipmentOptions {
        mainStats {
          stat
          isRange
          value
          minValue
          maxValue
        }
        subStats {
          stat
          isRange
          value
          minValue
          maxValue
        }
        lb5Option
        lb5Value
        maxSubOptionLines                  
      }
      costume {
        illustration {
          node {
            sourceUrl
          }
        }
        hero {
          nodes {
            id
          }
        }
      }
      itemInformation {
        achievement
        artifactDescription
        artifactPassives
        artifactRarity
        battleMedalShopCost
        bottleCapCost
        collections {
          nodes {
            id
          }
        }
        cost
        costumeWeaponType
        equipmentShopCost
        howToObtain
        itemType {
          nodes {
            name
          }
        }
        maxLevel
        mileageShopCost
        mirrorShardCost
        mysticThreadCost
        rarity
        unreleased
      }
    }
  }
}
'''