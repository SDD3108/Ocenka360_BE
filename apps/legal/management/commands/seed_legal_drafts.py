from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.legal.models import LegalDocument

TEXTS = {
    "ru": (
        "ТЕСТОВЫЙ ЧЕРНОВИК — не для реальных клиентов. Юридическое наименование ИП ещё не подтверждено. ",
        {
            "terms": (
                "Пользовательское соглашение — черновик",
                "Тестовый сервис Оценка 360 предназначен для совершеннолетних пользователей. Пользователь подтверждает владение email и отвечает за передаваемые сведения. Код входа нельзя передавать другим лицам. В Part 2 услуги оценки и реальные платежи недоступны. Официальный отчёт впоследствии утверждает оценщик. Итоговые условия услуг и возвратов будут опубликованы до их запуска.",
            ),
            "privacy": (
                "Политика конфиденциальности — черновик",
                "Для тестирования используются email, имя, необязательный телефон, выбранный язык, технические сессии и записи принятия документов. Коды хранятся в виде HMAC; в локальном console-режиме текст письма виден разработчику. Реальные удостоверения и клиентские отчёты в тестовый сервис не загружать. Точные реквизиты оператора, основания, сроки хранения и порядок обращений должны быть утверждены до публикации. Контакт: sati-ocenka@mail.ru.",
            ),
            "personal_data": (
                "Согласие на обработку данных — черновик",
                "При тестировании пользователь отдельно и явно подтверждает обработку введённых тестовых сведений для создания аккаунта, входа и хранения выбранных настроек. Это не согласие на рекламу, передачу документов третьих лиц или отправку данных в Gemini. Время, версия и контрольная сумма принятого текста сохраняются. Производственный текст требует утверждения владельцем ИП до начала реального сбора данных.",
            ),
        },
    ),
    "kk": (
        "СЫНАҚ ЖОБАСЫ — нақты клиенттерге арналмаған. ЖК-нің заңды атауы әлі расталмаған. ",
        {
            "terms": (
                "Пайдаланушы келісімі — жоба",
                "Ocenka 360 сынақ сервисі кәмелетке толған пайдаланушыларға арналған. Пайдаланушы email мекенжайын растайды және берген мәліметтеріне жауап береді. Кіру кодын басқаға беруге болмайды. Part 2 кезеңінде нақты бағалау қызметтері мен төлемдер жоқ. Ресми есепті бағалаушы бекітеді. Қызмет пен қайтарымның соңғы шарттары іске қосылғанға дейін жарияланады.",
            ),
            "privacy": (
                "Құпиялық саясаты — жоба",
                "Сынақ үшін email, аты-жөні, міндетті емес телефон, тіл, техникалық сессиялар және келісім жазбалары пайдаланылады. Кодтар HMAC түрінде сақталады. Жергілікті console режимінде хат мәтінін әзірлеуші көреді. Нақты жеке куәліктер мен клиент есептерін жүктемеңіз. Оператор деректемелері, сақтау мерзімдері және өтініш тәртібі жариялаудан бұрын бекітілуі тиіс. Байланыс: sati-ocenka@mail.ru.",
            ),
            "personal_data": (
                "Деректерді өңдеуге келісім — жоба",
                "Сынақ кезінде пайдаланушы аккаунт ашу, кіру және баптауларды сақтау үшін енгізілген сынақ деректерін өңдеуге жеке келісім береді. Бұл жарнамаға, өзге тұлғалардың құжаттарын беруге немесе Gemini-ге деректер жіберуге келісім емес. Қабылданған мәтіннің уақыты, нұсқасы және бақылау сомасы сақталады. Нақты деректерді жинауға дейін соңғы мәтінді ЖК иесі бекітуі қажет.",
            ),
        },
    ),
    "en": (
        "TEST DRAFT — not for real customers. The sole proprietor's legal name is not yet confirmed. ",
        {
            "terms": (
                "Terms of use — draft",
                "The Ocenka 360 test service is for adults. Users verify their email and are responsible for submitted information. Never share sign-in codes. Part 2 does not provide real valuation services or payments. Official reports will require appraiser approval. Final service and refund terms must be published before launch.",
            ),
            "privacy": (
                "Privacy policy — draft",
                "Test data includes email, names, optional phone, language, technical sessions and consent records. Codes are stored as HMAC digests; local console email exposes the message to the developer. Do not upload real identity documents or customer reports. Operator details, legal grounds, retention and contact procedures must be approved before publication. Contact: sati-ocenka@mail.ru.",
            ),
            "personal_data": (
                "Data processing consent — draft",
                "For testing, users explicitly accept processing of submitted test information for account creation, sign-in and settings. This is not consent to marketing, processing documents of other persons, or sending data to Gemini. Acceptance time, document version and checksum are recorded. The proprietor must approve the production text before real data collection begins.",
            ),
        },
    ),
}


class Command(BaseCommand):
    help = "Create explicitly marked local-only legal drafts; never overwrite an existing version."

    def handle(self, *args, **options):
        if not settings.ALLOW_DRAFT_LEGAL_DOCUMENTS:
            raise CommandError("Draft legal documents are disabled in this environment.")
        created = 0
        for language, (prefix, documents) in TEXTS.items():
            for kind, (title, content) in documents.items():
                exists = LegalDocument.objects.filter(kind=kind, language=language, is_active=True).exists()
                _, new = LegalDocument.objects.get_or_create(
                    kind=kind,
                    language=language,
                    version="dev-1",
                    defaults={
                        "title": title,
                        "content": prefix + content,
                        "is_draft": True,
                        "is_active": not exists,
                    },
                )
                created += int(new)
        self.stdout.write(
            self.style.SUCCESS(f"Created {created} local test drafts. Not approved legal documents.")
        )
