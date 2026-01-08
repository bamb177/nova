// Zone Nova Rune Sets Data
// Centralized rune data to avoid hardcoding in builds

export const RUNE_SETS = {
  Alpha: {
    name: 'Alpha',
    image: 'Alpha.jpg',
    twoPiece: '공격력 +8%',
    fourPiece: '기본 공격 피해 +30%',
  },
  'Shattered-Foundation': {
    name: 'Shattered Foundation',
    image: 'Shattered-Foundation.jpg',
    twoPiece: '방어력 +12%',
    fourPiece: '보호막 효과 +20%',
  },
  Beth: {
    name: 'Beth',
    image: 'Beth.jpg',
    twoPiece: '치명타 확률 +6%',
    fourPiece: 'HP가 80% 이상일 때 치명타 피해 +24%',
  },
  Zahn: {
    name: 'Zahn',
    image: 'Zahn.jpg',
    twoPiece: 'HP +8%',
    fourPiece: '궁극기 사용 후 받는 피해 5% 감소 (10초)',
  },
  Daleth: {
    name: 'Daleth',
    image: 'Daleth.jpg',
    twoPiece: '회복 효과 +10%',
    fourPiece: '전투 시작 시 즉시 에너지 1 획득',
  },
  Epsilon: {
    name: 'Epsilon',
    image: 'Epsilon.jpg',
    twoPiece: '추가 공격 피해 +20%',
    fourPiece: '궁극기 사용 후 아군 전체의 피해가 10% 증가하며, 10초간 지속',
    note: '동일한 패시브 효과는 중첩되지 않음',
  },
  Hert: {
    name: 'Hert Extra Attack Damage',
    image: 'Hert-Pursuit-Damage.jpg',
    twoPiece: '추가 공격 피해 +20%',
    fourPiece: '추가 공격 피해를 가한 후 치명타 확률 +15% (10초)',
//    note: '길드 레이드에서만 획득 가능',
  },
  'Gimel-Continuous-Damage': {
    name: 'Gimel Continuous Damage',
    image: 'Gimel-Continuous-Damage.jpg',
    twoPiece: '지속 피해 +20%',
    fourPiece: '지속 피해를 가한 후 자신의 공격력이 2% 증가하며 최대 10중첩, 5초간 지속',
 //   note: '길드 레이드에서만 획득 가능',
  },
  Giants: {
    name: 'Giants [Vulnerability]',
    image: 'Giants.jpg',
    twoPiece: '공격력 +8%',
    fourPiece:
      '장착 캐릭터가 디버퍼 클래스일 경우, 궁극기 피해를 받은 대상이 5초간 받는 피해 10% 증가',
    classRestriction: 'Debuffer',
    note: '동일 효과 중첩 불가. 길드 레이드에서만 획득 가능',
  },
  Tide: {
    name: 'Tide [Energy]',
    image: 'Tide.jpg',
    twoPiece: '방어력 +12%',
    fourPiece: '전투 시작 후 10초 동안 아군 전체의 에너지 획득 효율 +30%',
    note: '효과 중첩 불가. 파티 내 Daleth 4세트 효과는 비활성화됨. 길드 레이드 전용',
  },

  // HP: {
  //   name: 'HP',
  //   image: 'HP.jpg',
  //   twoPiece: 'HP +10%',
  //   fourPiece: 'HP 추가 +15%',
  // },
  // DEF: {
  //   name: 'Defense',
  //   image: 'DEF.jpg',
  //   twoPiece: '방어력 +10%',
  //   fourPiece: '방어력 추가 +15%',
  // },
};

// Main stats by rune position (fixed for all characters)
export const MAIN_STATS_BY_POSITION = {
  1: {
    name: 'Position 1 — Fixed Main Stat',
    stat: 'HP (Flat Value)',
    description: 'Always HP - no other options',
    isFixed: true,
  },
  2: {
    name: 'Position 2 — Fixed Main Stat',
    stat: 'Attack (Flat Value)',
    description: 'Always Attack - no other options',
    isFixed: true,
  },
  3: {
    name: 'Position 3 — Fixed Main Stat',
    stat: 'Defense (Flat Value)',
    description: 'Always Defense - no other options',
    isFixed: true,
  },
  4: {
    name: 'Position 4 — Variable Main Stats',
    availableStats: [
      'Healing Effectiveness (%)',
      'Critical Rate (%)',
      'Critical Damage (%)',
      'Attack Penetration (%)',
      'Attack (%)',
      'HP (%)',
      'Defense (%)',
    ],
    isFixed: false,
  },
  5: {
    name: 'Position 5 — Variable Main Stats',
    availableStats: [
      'Wind Attribute Damage (%)',
      'Fire Attribute Damage (%)',
      'Ice Attribute Damage (%)',
      'Holy Attribute Damage (%)',
      'Chaos Attribute Damage (%)',
      'Attack (%)',
      'HP (%)',
      'Defense (%)',
    ],
    isFixed: false,
  },
  6: {
    name: 'Position 6 — Variable Main Stats',
    availableStats: ['Attack (%)', 'HP (%)', 'Defense (%)'],
    isFixed: false,
  },
};

// Helper to get rune set info
export function getRuneSet(runeKey) {
  return RUNE_SETS[runeKey] || null;
}

// Helper to build rune set recommendation with correct effects
export function buildRuneRecommendation(mainRuneKey, secondaryRuneKey, options = {}) {
  const mainRune = RUNE_SETS[mainRuneKey];
  const secondaryRune = RUNE_SETS[secondaryRuneKey];

  if (!mainRune || !secondaryRune) {
    console.warn(`Rune not found: ${!mainRune ? mainRuneKey : secondaryRuneKey}`);
    return null;
  }

  return {
    name: `${mainRune.name} 4세트 + ${secondaryRune.name} 2세트`,
    englishName: `${mainRune.name} 4-piece + ${secondaryRune.name} 2-piece`,
    mainRune: mainRuneKey,
    secondaryRune: secondaryRuneKey,
    mainRune2Piece: `2세트: ${mainRune.twoPiece}`,
    mainRune4Piece: `4세트: ${mainRune.fourPiece}`,
    secondaryRuneEffect: `2세트: ${secondaryRune.twoPiece}`,
    ...options,
  };
}

export default RUNE_SETS;
