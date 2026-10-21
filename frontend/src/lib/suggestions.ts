/** Curated, source-tested starters. Labels stay short; questions preserve retrieval context. */
type LocalizedSuggestion = {
  label: string;
  question: string;
};

type Suggestion = {
  id: string;
  sq: LocalizedSuggestion;
  en: LocalizedSuggestion;
};

export type SelectedSuggestion = { id: string } & LocalizedSuggestion;
export const SUGGESTION_COUNT = 4;

export const suggestionBank: readonly Suggestion[] = [
  {
    id: "movement",
    sq: { label: "Revolucioni Flamingo?", question: "Çfarë është Revolucioni Flamingo?" },
    en: {
      label: "Flamingo Revolution?",
      question: "What is the Flamingo Revolution? Answer in English.",
    },
  },
  {
    id: "diaspora",
    sq: { label: "Diaspora Zbarkon?", question: "Çfarë është Diaspora Zbarkon?" },
    en: { label: "Diaspora Zbarkon?", question: "What is Diaspora Zbarkon? Answer in English." },
  },
  {
    id: "pulsi",
    sq: { label: "Çfarë tregon Pulsi?", question: "Çfarë tregon Pulsi i protestës?" },
    en: {
      label: "What does Pulsi show?",
      question: "What does the Pulsi protest participation index measure? Answer in English.",
    },
  },
  {
    id: "peak_day",
    sq: {
      label: "Dita më e madhe?",
      question: "Cila ishte dita më e madhe sipas indeksit Pulsi?",
    },
    en: {
      label: "Peak protest day?",
      question: "Which day scored 100 on the Pulsi index? Answer in English.",
    },
  },
  {
    id: "referendum",
    sq: { label: "Referendumi 21/2024?", question: "Çfarë është Referendumi 21/2024?" },
    en: {
      label: "Referendum 21/2024?",
      question: "What is Referendum 21/2024? Answer in English.",
    },
  },
  {
    id: "protest_map",
    sq: {
      label: "Sa qytete në hartë?",
      question: "Sa qytete dhe shtete ka Harta e Protestave?",
    },
    en: {
      label: "Cities on the map?",
      question: "How many cities and countries are on Harta e Protestave? Answer in English.",
    },
  },
  {
    id: "vlora_airport",
    sq: { label: "Aeroporti i Vlorës?", question: "Çfarë ndodhi me Aeroportin e Vlorës?" },
    en: {
      label: "Vlora Airport?",
      question: "What happened with Vlora Airport? Answer in English.",
    },
  },
  {
    id: "diaspora_help",
    sq: { label: "Si ndihmon diaspora?", question: "Si mund të ndihmojë diaspora?" },
    en: {
      label: "How can diaspora help?",
      question: "How can the Albanian diaspora help? Answer in English.",
    },
  },
  {
    id: "dossier",
    sq: { label: "Dosja Flamingo?", question: "Çfarë është Dosja Flamingo?" },
    en: {
      label: "Flamingo Dossier?",
      question: "What is the Flamingo Dossier? Answer in English.",
    },
  },
  {
    id: "zvernec",
    sq: { label: "Çfarë ndodhi në Zvërnec?", question: "Çfarë ndodhi në Zvërnec?" },
    en: {
      label: "What happened in Zvërnec?",
      question: "What happened in Zvërnec? Answer in English.",
    },
  },
  {
    id: "why_protesting",
    sq: {
      label: "Pse protestojnë shqiptarët?",
      question: "Pse protestojnë shqiptarët me Revolucionin Flamingo?",
    },
    en: {
      label: "Why are Albanians protesting?",
      question: "Why are Albanians protesting with the Flamingo Revolution? Answer in English.",
    },
  },
  {
    id: "protest_day_140",
    sq: {
      label: "Çfarë ndodhi më 17 tetor?",
      question: "Çfarë ndodhi në ditën 140 të protestës, më 17 tetor 2026?",
    },
    en: {
      label: "What happened on 17 October?",
      question: "What happened on protest day 140, 17 October 2026? Answer in English.",
    },
  },
  {
    id: "police_response",
    sq: {
      label: "Si reagoi policia?",
      question:
        "Çfarë raportojnë burimet për veprimet e policisë gjatë protestave në Tiranë më 13–17 tetor 2026?",
    },
    en: {
      label: "How did police respond?",
      question:
        "What do sources report about police actions during the Tirana protests on 13–17 October 2026? Answer in English.",
    },
  },
  {
    id: "protest_safety",
    sq: {
      label: "Siguria në protestë?",
      question:
        "Si të qëndroj i sigurt dhe të largohem nga rreziku nëse një protestë në Shqipëri bëhet e dhunshme?",
    },
    en: {
      label: "Stay safe at a protest?",
      question:
        "How can I stay safe and leave danger if a protest in Albania becomes violent? Answer in English.",
    },
  },
  {
    id: "free_legal_help",
    sq: {
      label: "Ku gjej avokat falas?",
      question:
        "Ku mund të gjej ndihmë juridike falas për protestuesit në Shqipëri? Më drejto te kontaktet në udhëzuesin e sigurisë të Diaspora Zbarkon: https://www.diaspora-zbarkon.com/siguria/.",
    },
    en: {
      label: "Free legal-help contacts?",
      question:
        "Where can protesters in Albania find free legal help? Link to the contacts in Diaspora Zbarkon's safety guide at https://www.diaspora-zbarkon.com/siguria/. Answer in English.",
    },
  },
  {
    id: "arrest_rights",
    sq: {
      label: "Të drejtat kur arrestohem?",
      question:
        "Çfarë të drejtash kam sipas ligjit shqiptar nëse policia më ndalon ose arreston, përfshirë identifikimin, heshtjen dhe ndihmën e avokatit?",
    },
    en: {
      label: "My rights if arrested?",
      question:
        "Under Albanian law, what are my rights if the police detain or arrest me, including identification requirements, remaining silent, and access to a lawyer? Answer in English.",
    },
  },
  {
    id: "protest_preparation",
    sq: {
      label: "Çfarë të marr në protestë?",
      question:
        "Çfarë duhet të marr me vete për të marrë pjesë në mënyrë paqësore dhe të sigurt në një protestë në Shqipëri?",
    },
    en: {
      label: "What should I bring?",
      question:
        "What should I bring to participate peacefully and safely in a protest in Albania? Answer in English.",
    },
  },
  {
    id: "document_abuses",
    sq: {
      label: "Si të dokumentoj shkeljet?",
      question:
        "Si mund të dokumentoj në mënyrë të sigurt shkeljet gjatë një proteste në Shqipëri, duke mbrojtur privatësinë time dhe të të tjerëve?",
    },
    en: {
      label: "Document abuses safely?",
      question:
        "How can I safely document abuses during a protest in Albania while protecting my privacy and other people's privacy? Answer in English.",
    },
  },
  {
    id: "detained_relative",
    sq: {
      label: "Familjar i ndaluar?",
      question:
        "Çfarë hapash mund të ndërmarr nëse një familjar ndalohet ose arrestohet gjatë një proteste në Shqipëri dhe ku mund të kërkoj ndihmë juridike?",
    },
    en: {
      label: "A relative was detained?",
      question:
        "What steps can I take if a relative is detained or arrested during a protest in Albania, and where can I find legal help? Answer in English.",
    },
  },
];

/** Pick four different prompts in random positions when an empty chat opens. */
export function selectSuggestions(
  pageLanguage: string,
  random: () => number = Math.random,
): SelectedSuggestion[] {
  const locale = /^en(?:-|$)/i.test(pageLanguage.trim()) ? "en" : "sq";
  const pool = [...suggestionBank];
  for (let index = 0; index < SUGGESTION_COUNT; index += 1) {
    const swapIndex = index + Math.floor(random() * (pool.length - index));
    [pool[index], pool[swapIndex]] = [pool[swapIndex], pool[index]];
  }
  return pool.slice(0, SUGGESTION_COUNT).map((entry) => ({ id: entry.id, ...entry[locale] }));
}
