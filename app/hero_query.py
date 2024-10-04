hero_query = '''
        query GetAllHeroes {
            heroes(first: 1000) {
                nodes {
                id
                databaseId
                uri
                slug
                featuredImage {
                    node {
                    mediaDetails {
                        sizes(include: THUMBNAIL) {
                        sourceUrl
                        name
                        }
                    }
                    }
                }
                heroId
                title
                heroInformation {
                    portrait {
                    art {
                        node {
                        sourceUrl
                        }
                    }
                    title
                    }
                    bioFields {
                    rarity
                    element
                    role
                    name
                    age
                    compatibleEquipment
                    exclusiveWeapon {
                        nodes {
                        ... on Item {
                            id
                            title
                            weapons { 
                            maxDps
                            element
                            exclusiveEffects
                            exclusive
                            magazine
                            maxAtk
                            minAtk
                            minDps
                            weaponSkill
                            weaponSkillAtk
                            weaponSkillChain
                            weaponSkillDescription
                            weaponSkillName
                            weaponSkillRegenTime
                            weaponSkillVideo {
                                node {
                                mediaItemUrl
                                }
                            }
                            weaponType
                            engraving {
                                value
                                stat
                            }
                            }
                            featuredImage {
                            node {
                                sourceUrl
                            }
                            }
                            equipmentSubOptions {
                            subOptions
                            fieldGroupName
                            maxLines
                            subAtk
                            subAtkOnKill
                            subBasicTypeAtk
                            subCritChance
                            subDamageReduction
                            subDarkTypeAtk
                            subDef
                            subDefFlat
                            subEarthTypeAtk
                            subFireTypeAtk
                            subHealFlat
                            subHealPercent
                            subHp
                            subHpOnKill
                            subLightTypeAtk
                            subShieldOnKill
                            subShieldOnStart
                            subSkillDamage
                            subSkillRegenOnKill
                            subSkillRegenSpeed
                            subWaterTypeAtk
                            }
                            equipmentOptions {
                            options
                            atk
                            atkOnKill
                            basicTypeAtk
                            critChance
                            critHitMultiplier
                            damageReduction
                            darkTypeAtk
                            decreaseDamageTakenBySkill
                            def
                            defFlat
                            earthTypeAtk
                            extraDamageType
                            fieldGroupName
                            fireTypeAtk
                            healFlat
                            healPercent
                            hp
                            hpOnKill
                            increaseDamageAmount
                            increaseDamageCondition
                            increaseDamageThreshold
                            increaseDamageToTanks
                            lb5Option
                            lb5Value
                            lightTypeAtk
                            minDamageReduction
                            minDefFlat
                            onHitDamage
                            onHitDamageSeconds
                            onHitHealAllies
                            onHitHealSeconds
                            shieldOnKill
                            shieldOnStart
                            skillDamage
                            skillRegenOnKill
                            skillRegenSpeed
                            waterTypeAtk
                            }
                            itemInformation {
                            rarity
                            }
                        }
                        }
                    }
                    weight
                    story
                    species
                    naReleaseDate
                    krReleaseDate
                    jpReleaseDate
                    height
                    }
                    abilityFields {
                    chainStateTrigger
                    chainStateResult
                    partyBuff {
                        affectsParty
                        value
                        stat
                    }
                    chainSkillDescription
                    chainSkillName
                    normalAtkDescription
                    normalAtkName
                    specialAbilityName
                    specialAbilityDescription
                    }
                    portrait {
                    art {
                        node {
                        sourceUrl
                        }
                    }
                    title
                    }
                    thumbnail {
                    node {
                        sourceUrl
                    }
                    }
                    background {
                    node {
                        sourceUrl
                    }
                    }
                    mlbAwakeningFields {
                    midGradeHpMlb
                    midGradeDreamMlb
                    midGradeDefMlb
                    midGradeAtkMlb
                    lowGradeHpMlb
                    lowGradeDreamMlb
                    lowGradeDefMlb
                    lowGradeAtkMlb
                    legendaryAwakeningMlb
                    highGradeHpMlb
                    highGradeDreamMlb
                    highGradeDefMlb
                    highGradeAtkMlb
                    gold
                    }
                    statFields {
                    atk
                    basicResistance
                    cardSlot
                    crit
                    damageReduction
                    darkResistance
                    def
                    earthResistance
                    fieldGroupName
                    heal
                    hp
                    lightResistance
                    waterResistance
                    fireResistance
                    atkRank
                    defRank
                    hpRank
                    critRank
                    healRank
                    drRank
                    heroCount
                    }
                    statPriorities {
                    statPriority {
                        edges {
                        node {
                            ... on Post {
                            id
                            title
                            }
                        }
                        }
                    }
                    buildName
                    explanation
                    }
                    portrait2 {
                    art {
                        node {
                        sourceUrl
                        title
                        }
                    }
                    }
                    costumes {
                    edges {
                        node {
                        ... on Item {
                            id
                            uri
                            featuredImage {
                            node {
                                sourceUrl
                            }
                            }
                            title
                        }
                        }
                    }
                    }
                    evolutionFields2 {
                    evolution1 {
                        node {
                        sourceUrl
                        }
                    }
                    evolution2 {
                        node {
                        sourceUrl
                        }
                    }
                    evolution3 {
                        node {
                        sourceUrl
                        }
                    }
                    evolution4 {
                        node {
                        sourceUrl
                        }
                    }
                    evolution5 {
                        node {
                        sourceUrl
                        }
                    }
                    }
                    evolutionFields {
                    evolution1 {
                        node {
                        sourceUrl
                        }
                    }
                    evolution2 {
                        node {
                        sourceUrl
                        }
                    }
                    evolution3 {
                        node {
                        sourceUrl
                        }
                    }
                    evolution5 {
                        node {
                        sourceUrl
                        }
                    }
                    evolution4 {
                        node {
                        sourceUrl
                        }
                    }
                    }
                    evaluationFields {
                    pros
                    cons
                    tags
                    }
                    buildGuideFields {
                    suggestedWeapons {
                        suggestedWeapon {
                        edges {
                            node {
                            ... on Item {
                                id
                                uri
                                title
                                featuredImage {
                                node {
                                    sourceUrl
                                }
                                }
                            }
                            }
                        }
                        }
                        weaponExplanation
                    }
                    suggestedShield {
                        suggestedShield {
                        edges {
                            node {
                            ... on Item {
                                id
                                uri
                                title
                                featuredImage {
                                node {
                                    sourceUrl
                                }
                                }
                            }
                            }
                        }
                        }
                        shieldExplanation
                    }
                    suggestedMerch {
                        merchExplanation
                        suggestedMerch {
                        edges {
                            node {
                            ... on Item {
                                id
                                uri
                                title
                                featuredImage {
                                node {
                                    sourceUrl
                                }
                                }
                            }
                            }
                        }
                        }
                    }
                    suggestedCards {
                        cardExplanation
                        suggestedCard
                    }
                    suggestedAccessories {
                        accessoryExplanation
                        suggestedAccessory {
                        edges {
                            node {
                            ... on Item {
                                id
                                uri
                                title
                                featuredImage {
                                node {
                                    sourceUrl
                                }
                                }
                            }
                            }
                        }
                        }
                    }
                    }
                    bioFields2 {
                    weight
                    story
                    name
                    height
                    age
                    }
                    analysisFields {
                    detailedReview
                    }
                    ascentAbilities {
                    chainSkillDescription
                    chainSkillName
                    chainStateResult
                    chainStateTrigger
                    normalAtkDescription
                    normalAtkName
                    partyBuff {
                        affectsParty
                        stat
                        value
                    }
                    specialAbilityDescription
                    specialAbilityName
                    }
                    ascentExAbilities {
                    chainSkillDescription
                    chainSkillName
                    chainStateResult
                    chainStateTrigger
                    normalAtkDescription
                    normalAtkName
                    partyBuff {
                        affectsParty
                        stat
                        value
                    }
                    specialAbilityDescription
                    specialAbilityName
                    }
                    exAbilities {
                    chainSkillDescription
                    chainSkillName
                    chainStateResult
                    chainStateTrigger
                    normalAtkDescription
                    normalAtkName
                    partyBuff {
                        affectsParty
                        stat
                        value
                    }
                    specialAbilityDescription
                    specialAbilityName
                    }
                    variations
                    illustrations {
                    name
                    image {
                        node {
                        title
                        sourceUrl
                        }
                    }
                    }
                }
                }
            }
            } 
        '''