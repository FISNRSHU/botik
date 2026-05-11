import re
from typing import List

class ProfanityFilter:
    """Customizable profanity filter with regex detection and replacement."""

    DEFAULT_WORD_LIST = [
        # Ukrainian & Russian strong profanity
        "хуй", "хуя", "хую", "хує", "хуї", "пізда", "пізди", "пізду", "пізде", "пизда", "пизди", "пизду", "пизде",
        "бля", "блять", "бляд", "бляді", "блядина", "блядство", "сука", "суки", "суку", "сукою", "сучка", "сучонок",
        "лох", "лоха", "лоху", "лохи", "лохів", "мудак", "мудака", "мудаку", "мудаки", "мудаків", "мудило",
        "гівно", "говно", "гівна", "гівну", "гівні", "срака", "сраку", "сраці", "срачка", "срати", "насрати",
        "член", "члена", "члену", "члени", "піся", "пісюн", "писюн", "залупа", "залупе", "залупі", "залупу", "манда", "манди",
        "писька", "письку", "письки", "ебан", "ебать", "ебало", "ебло", "йоба", "йобаний", "йобана", "йобане", "йобнутий",
        "підар", "підорас", "підарас", "петух", "гандон", "гандона", "гандону", "гандонів", "шлюха", "шлюху", "шлюхи", "шлюхою",
        "курва", "курви", "курву", "курвою", "курватня", "проститутка", "повія", "бздун", "бздюх", "пердун", "пердільник",
        "довбойоб", "довбень", "дебил", "дебіл", "ідіот", "ідіотка", "кретин", "даун", "овца", "баран", "осел",
        "тупиця", "тупінь", "тупий", "безглуздий", "виблядок", "ублюдок", "вилупок", "нелюд", "мразь", "мраза", "мразота",
        "тварюка", "тварина", "гад", "гадина", "гадюка", "гнида", "гниди", "покидок", "відморозок", "отморозок", "козел", "козлина",
        "свиня", "свинособака", "шакал", "шавка", "паршивець", "покидьок", "виродок", "убогий", "урод", "уродина", "страховидло",
        "недоумок", "недоносок", "нероба", "ледар", "бездар", "бездарь", "невдаха", "лузер",
        # English profanity
        "fuck", "fuсk", "f1ck", "shit", "shitt", "sh1t", "bitch", "b1tch", "asshole", "assh0le", "dick", "d1ck",
        "pussy", "cunt", "whore", "slut", "cock", "bastard", "motherfucker", "mf", "mthrfckr", "damn", "goddamn",
        "hell", "bloody", "crap", "darn", "heck", "piss", "piss off", "screw", "sucker", "wanker", "twat", "knob",
        "prat", "bugger", "arse", "arsehole", "bollocks", "bloody hell", "goddam",
        # Common phrases
        "пішов на хуй", "йди на хуй", "іди нахуй", "на хуй", "нахуй", "похуй", "похую", "не похуй", "в пізду", "до пізди",
        "розпіздяй", "хуйня", "хуйовий", "херня", "херовий", "фігня", "фіговий", "хрінь", "хріновий", "заєбісь",
        "заєба", "заїба", "заєбатий", "заєбіс", "заєбу", "в'їбати", "виїбати", "проїбати", "наїбати", "об'їбати", "переїбати",
        "приїбатися", "роз'їбати", "уїбати", "від'їбати", "доїбатися", "дойоба", "дойобист", "виєбон", "виєбонутися",
        "приєбнути", "приєбнутися", "роз'єб", "проєб", "наєб", "наєбка", "наєбник", "об'єб", "переобути", "кинути на гроші",
    ]

    def __init__(self, custom_word_list: List[str] = None):
        """
        Initialize profanity filter.
        :param custom_word_list: Additional words to add to default list.
        """
        word_list = self.DEFAULT_WORD_LIST[:]
        if custom_word_list:
            word_list.extend(custom_word_list)
        # Build regex pattern once to improve performance
        pattern = r'(^|[\s\.\,\!\?\-])({})([\s\.\,\!\?\-]|$)'.format('|'.join(re.escape(word) for word in word_list))
        self._regex = re.compile(pattern, re.IGNORECASE | re.UNICODE)

    def contains_profanity(self, text: str) -> bool:
        """Check if text contains any profanity."""
        return bool(self._regex.search(text))

    def censor(self, text: str, replace_char: str = '*') -> str:
        """
        Replace profanity with given character.
        :param text: Input text.
        :param replace_char: Character to replace with.
        :return: Censored text.
        """
        def repl(match):
            left, word, right = match.group(1), match.group(2), match.group(3)
            return left + (replace_char * len(word)) + right
        return self._regex.sub(repl, text)