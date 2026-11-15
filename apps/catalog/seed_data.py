"""Editable starting catalog from owner requirements and ocenka360.kz, not a legal checklist."""

from copy import deepcopy


def tr(ru, kk, en):
    return {"ru": ru, "kk": kk, "en": en}


def choice(key, label, options, **kwargs):
    return {
        "key": key,
        "type": "choice",
        "label": label,
        "required": True,
        "options": [{"value": v, "label": text} for v, text in options],
        **kwargs,
    }


def field(key, kind, label, required=True, **kwargs):
    return {"key": key, "type": kind, "label": label, "required": required, **kwargs}


def when(key, value):
    return {"field": key, "equals": value}


NOTICE = tr(
    "Это предварительная анкета. Перечень документов и возможность удалённого осмотра подтверждает оценщик. Итоговую стоимость услуги утверждает оценщик. Проверка анкеты не создаёт заявку и не подтверждает оплату.",
    "Бұл алдын ала сауалнама. Құжаттар тізімін және қашықтан тексеру мүмкіндігін бағалаушы растайды. Қызметтің түпкілікті бағасын бағалаушы бекітеді. Сауалнаманы тексеру өтінім жасамайды және төлемді растамайды.",
    "This is a preliminary questionnaire. The appraiser confirms the document list, remote inspection availability and final service fee. Validating this form does not create an order or confirm payment.",
)
GENERAL_HELP = tr(
    "Подготовьте документ для проверки оценщиком; дополнительные требования уточняются по заявке.",
    "Құжатты бағалаушының тексеруіне дайындаңыз; қосымша талаптар өтінім бойынша нақтыланады.",
    "Prepare this document for the appraiser's review; additional requirements depend on the case.",
)


def document(key, title, required=True, condition=None, help_text=None):
    result = {"key": key, "title": title, "required": required, "help": help_text or GENERAL_HELP}
    if condition:
        result["when"] = condition
    return result


CATEGORIES = [
    ("real-estate", tr("Недвижимость", "Жылжымайтын мүлік", "Real estate")),
    ("damage", tr("ДТП и ущерб", "ЖКО және залал", "Accidents and damage")),
    ("movable", tr("Движимое имущество", "Жылжымалы мүлік", "Movable property")),
    ("business", tr("Бизнес и активы", "Бизнес және активтер", "Business and assets")),
    (
        "public-purposes",
        tr("Для государственных органов", "Мемлекеттік органдар үшін", "Public authority purposes"),
    ),
    ("court", tr("Судебно-оценочная экспертиза", "Сот-бағалау сараптамасы", "Court-related valuation")),
    ("intangible", tr("Нематериальные активы", "Материалдық емес активтер", "Intangible assets")),
    ("accounting", tr("Бухучёт и налоги", "Бухгалтерлік есеп және салықтар", "Accounting and tax purposes")),
    ("biological", tr("Биологические активы", "Биологиялық активтер", "Biological assets")),
    ("other", tr("Другая задача", "Басқа міндет", "Other valuation needs")),
]
# The last value selects a form template, not a pricing algorithm.
SERVICES = [
    (
        "apartment",
        "real-estate",
        tr("Оценка квартиры", "Пәтерді бағалау", "Apartment valuation"),
        "apartment",
    ),
    ("house", "real-estate", tr("Оценка дома", "Үйді бағалау", "House valuation"), "house"),
    (
        "land",
        "real-estate",
        tr("Оценка земельного участка", "Жер учаскесін бағалау", "Land valuation"),
        "land",
    ),
    (
        "commercial-property",
        "real-estate",
        tr("Оценка нежилого объекта", "Тұрғын емес нысанды бағалау", "Commercial property valuation"),
        "commercial",
    ),
    ("garage", "real-estate", tr("Оценка гаража", "Гаражды бағалау", "Garage valuation"), "commercial"),
    (
        "accident-damage",
        "damage",
        tr("Оценка ущерба после ДТП", "ЖКО залалын бағалау", "Accident damage valuation"),
        "accident",
    ),
    (
        "property-damage",
        "damage",
        tr("Оценка ущерба имуществу", "Мүлікке келтірілген залалды бағалау", "Property damage valuation"),
        "damage",
    ),
    ("vehicle", "movable", tr("Оценка автомобиля", "Автокөлікті бағалау", "Vehicle valuation"), "vehicle"),
    (
        "equipment",
        "movable",
        tr(
            "Оценка оборудования и спецтехники",
            "Жабдық пен арнайы техниканы бағалау",
            "Equipment and machinery valuation",
        ),
        "equipment",
    ),
    ("inventory", "movable", tr("Оценка запасов", "Қорларды бағалау", "Inventory valuation"), "inventory"),
    (
        "business-share",
        "business",
        tr("Оценка бизнеса и долей", "Бизнес пен үлестерді бағалау", "Business and share valuation"),
        "business",
    ),
    (
        "enterprise",
        "business",
        tr("Оценка имущественного комплекса", "Мүліктік кешенді бағалау", "Enterprise asset valuation"),
        "business",
    ),
    (
        "public-purpose",
        "public-purposes",
        tr(
            "Оценка для государственных органов",
            "Мемлекеттік органдар үшін бағалау",
            "Valuation for public authorities",
        ),
        "public",
    ),
    (
        "court-valuation",
        "court",
        tr("Судебно-оценочная экспертиза", "Сот-бағалау сараптамасы", "Court-related valuation"),
        "court",
    ),
    (
        "intangible-assets",
        "intangible",
        tr(
            "Оценка интеллектуальной собственности и НМА",
            "Зияткерлік меншік пен материалдық емес активтерді бағалау",
            "Intellectual property and intangible asset valuation",
        ),
        "intangible",
    ),
    (
        "accounting-tax",
        "accounting",
        tr(
            "Оценка для бухучёта и налоговой",
            "Есеп пен салық мақсатында бағалау",
            "Accounting and tax valuation",
        ),
        "accounting",
    ),
    (
        "livestock",
        "biological",
        tr("Оценка биологических активов", "Биологиялық активтерді бағалау", "Biological asset valuation"),
        "biological",
    ),
    (
        "custom-valuation",
        "other",
        tr("Индивидуальная задача оценки", "Жеке бағалау міндеті", "Custom valuation request"),
        "custom",
    ),
]


def definition_for(template):
    fields = [
        choice(
            "applicant_type",
            tr("Кто заказывает оценку?", "Бағалауға кім тапсырыс береді?", "Applicant type"),
            [
                ("individual", tr("Физическое лицо", "Жеке тұлға", "Individual")),
                ("company", tr("Организация", "Ұйым", "Company")),
            ],
        ),
        field(
            "company_name",
            "text",
            tr("Название организации", "Ұйым атауы", "Company name"),
            max_length=200,
            visible_if=when("applicant_type", "company"),
        ),
        choice(
            "applicant_role",
            tr("Ваше отношение к объекту", "Нысанға қатысыңыз", "Your role"),
            [
                ("owner", tr("Собственник", "Меншік иесі", "Owner")),
                ("representative", tr("Представитель", "Өкіл", "Representative")),
                ("other", tr("Другое основание", "Басқа негіз", "Other capacity")),
            ],
        ),
        choice(
            "purpose",
            tr("Цель оценки", "Бағалау мақсаты", "Purpose"),
            [
                ("sale", tr("Сделка", "Мәміле", "Transaction")),
                ("mortgage", tr("Залог / ипотека", "Кепіл / ипотека", "Collateral / mortgage")),
                ("court", tr("Суд / спор", "Сот / дау", "Court / dispute")),
                ("inheritance", tr("Наследство / дарение", "Мұра / сыйға тарту", "Inheritance / gift")),
                (
                    "insurance",
                    tr("Страхование / возмещение", "Сақтандыру / өтемақы", "Insurance / compensation"),
                ),
                ("accounting", tr("Бухучёт", "Бухгалтерлік есеп", "Accounting")),
                ("other", tr("Другая цель", "Басқа мақсат", "Other purpose")),
            ],
        ),
        field(
            "purpose_details",
            "textarea",
            tr("Уточните цель", "Мақсатты нақтылаңыз", "Describe the purpose"),
            max_length=1000,
            visible_if=when("purpose", "other"),
        ),
        field(
            "city",
            "text",
            tr("Город нахождения объекта", "Нысан орналасқан қала", "Asset location city"),
            max_length=100,
        ),
    ]
    docs = [
        document(
            "owner_id",
            tr(
                "Удостоверения собственников",
                "Меншік иелерінің жеке куәліктері",
                "Owners' identity documents",
            ),
            condition=when("applicant_type", "individual"),
            help_text=tr(
                "Перечень лиц и основание передачи их данных уточняет оценщик. Сейчас файлы не загружаются.",
                "Тұлғалар тізімін және олардың деректерін беру негізін бағалаушы нақтылайды. Қазір файлдар жүктелмейді.",
                "The appraiser confirms whose documents are needed and the basis for sharing their data. No files are uploaded at this stage.",
            ),
        ),
        document(
            "company_registration",
            tr(
                "Документ о регистрации организации",
                "Ұйымның тіркелу құжаты",
                "Company registration document",
            ),
            condition=when("applicant_type", "company"),
        ),
        document(
            "authority",
            tr(
                "Подтверждение полномочий представителя",
                "Өкіл өкілеттігін растау",
                "Representative authority document",
            ),
            condition=when("applicant_role", "representative"),
        ),
    ]
    if template in {"apartment", "house", "land", "commercial", "damage"}:
        fields.append(
            field(
                "address", "text", tr("Адрес объекта", "Нысан мекенжайы", "Property address"), max_length=300
            )
        )
    if template in {"apartment", "house", "land", "commercial"}:
        fields.append(
            field(
                "area_sqm",
                "decimal",
                tr("Площадь, м²", "Ауданы, м²", "Area, m²"),
                min="0.0001",
                max=100000000,
                help=tr(
                    "Укажите положительную площадь по документам (например, 65.5).",
                    "Құжаттағы ауданды көрсетіңіз (мысалы, 65.5).",
                    "Enter the area from documents, e.g. 65.5.",
                ),
            )
        )
        docs += [
            document(
                "ownership_basis",
                tr("Основание права собственности", "Меншік құқығының негізі", "Ownership basis document"),
            ),
            document(
                "encumbrance_f2",
                tr(
                    "Сведения об обременениях (Ф2)",
                    "Ауыртпалықтар туралы мәліметтер (Ф2)",
                    "Encumbrance information (F2)",
                ),
            ),
        ]
        if template != "land":
            docs.append(
                document(
                    "technical_passport",
                    tr("Технический паспорт", "Техникалық паспорт", "Technical passport"),
                )
            )
        if template in {"land", "house"}:
            docs.append(
                document(
                    "land_rights",
                    tr(
                        "Документы на земельный участок", "Жер учаскесінің құжаттары", "Land rights documents"
                    ),
                )
            )
        if template in {"apartment", "house"}:
            fields.append(
                field(
                    "rooms",
                    "integer",
                    tr("Количество комнат", "Бөлмелер саны", "Number of rooms"),
                    False,
                    min=1,
                    max=100,
                )
            )
    elif template in {"vehicle", "accident"}:
        fields += [
            field(
                "make_model",
                "text",
                tr("Марка и модель", "Маркасы мен моделі", "Make and model"),
                max_length=150,
            ),
            field(
                "manufacture_year",
                "integer",
                tr("Год выпуска", "Шығарылған жылы", "Manufacture year"),
                False,
                min=1886,
                max=2100,
            ),
        ]
        docs.append(
            document(
                "vehicle_registration",
                tr(
                    "Техпаспорт автомобиля",
                    "Автокөліктің техникалық паспорты",
                    "Vehicle registration document",
                ),
            )
        )
        if template == "accident":
            fields += [
                field("incident_date", "date", tr("Дата ДТП", "ЖКО күні", "Accident date")),
                field(
                    "damage_description",
                    "textarea",
                    tr("Описание повреждений", "Зақымдардың сипаттамасы", "Damage description"),
                    max_length=2000,
                ),
            ]
            docs.append(
                document(
                    "incident_materials",
                    tr(
                        "Материалы ДТП при наличии",
                        "ЖКО материалдары, бар болса",
                        "Available accident records",
                    ),
                    False,
                )
            )
    elif template == "damage":
        fields += [
            choice(
                "damage_type",
                tr("Причина ущерба", "Залал себебі", "Damage cause"),
                [
                    ("flood", tr("Залив", "Су басу", "Water damage")),
                    ("fire", tr("Пожар", "Өрт", "Fire")),
                    ("other", tr("Другое", "Басқа", "Other")),
                ],
            ),
            field("incident_date", "date", tr("Дата события", "Оқиға күні", "Incident date")),
            field(
                "damage_description",
                "textarea",
                tr("Описание ущерба", "Залал сипаттамасы", "Describe the damage"),
                max_length=2000,
            ),
        ]
        docs.append(
            document(
                "incident_record",
                tr(
                    "Акт о происшествии при наличии",
                    "Оқиға туралы акт, бар болса",
                    "Incident record if available",
                ),
                False,
            )
        )
    else:
        fields.append(
            field(
                "asset_description",
                "textarea",
                tr("Что необходимо оценить?", "Нені бағалау қажет?", "What needs valuation?"),
                max_length=2000,
            )
        )
        extras = {
            "equipment": (
                "equipment_list",
                tr(
                    "Перечень оборудования и характеристики",
                    "Жабдық тізімі мен сипаттамалары",
                    "Equipment list and specifications",
                ),
            ),
            "inventory": ("inventory_list", tr("Перечень запасов", "Қорлар тізімі", "Inventory list")),
            "business": (
                "financial_records",
                tr(
                    "Финансовая отчётность и сведения об активах",
                    "Қаржылық есептілік және активтер туралы мәліметтер",
                    "Financial statements and asset information",
                ),
            ),
            "public": (
                "authority_request",
                tr(
                    "Требования принимающего органа",
                    "Қабылдаушы органның талаптары",
                    "Receiving authority requirements",
                ),
            ),
            "court": (
                "court_materials",
                tr(
                    "Вопросы и материалы дела",
                    "Іс сұрақтары мен материалдары",
                    "Case materials and questions",
                ),
            ),
            "intangible": (
                "rights_description",
                tr(
                    "Сведения о правах на актив",
                    "Активке құқықтар туралы мәліметтер",
                    "Asset rights information",
                ),
            ),
            "accounting": (
                "asset_register",
                tr("Реестр основных средств", "Негізгі құралдар тізілімі", "Fixed asset register"),
            ),
            "biological": (
                "livestock_list",
                tr(
                    "Вид, количество и характеристики животных",
                    "Жануарлардың түрі, саны және сипаттамалары",
                    "Animal type, count and characteristics",
                ),
            ),
        }
        if template in extras:
            key, title = extras[template]
            docs.append(document(key, title, False))
        if template == "biological":
            fields.append(
                field(
                    "animal_count",
                    "integer",
                    tr("Количество животных", "Жануарлар саны", "Animal count"),
                    min=1,
                    max=10000000,
                )
            )
    if template in {"apartment", "house", "vehicle", "accident"}:
        fields.append(
            choice(
                "inspection_preference",
                tr("Предпочтительный осмотр", "Қалаған тексеру тәсілі", "Inspection preference"),
                [
                    ("onsite", tr("Выезд оценщика", "Бағалаушының келуі", "On-site visit")),
                    (
                        "remote",
                        tr(
                            "Фото клиента — после согласования",
                            "Клиент фотосуреттері — келісімнен кейін",
                            "Customer photos, subject to approval",
                        ),
                    ),
                ],
            )
        )
    fields += [
        choice(
            "urgency",
            tr("Срочность", "Шұғылдық", "Urgency"),
            [
                ("regular", tr("Обычный порядок", "Қалыпты тәртіп", "Regular")),
                (
                    "urgent",
                    tr(
                        "Срочно — согласовать возможность",
                        "Шұғыл — мүмкіндігін келісу",
                        "Urgent, subject to availability",
                    ),
                ),
            ],
        ),
        field("comment", "textarea", tr("Комментарий", "Пікір", "Comment"), False, max_length=2000),
    ]
    return deepcopy({"fields": fields, "documents": docs, "notice": NOTICE})


SUMMARIES = {
    "apartment": tr(
        "Анкета по квартире: адрес, площадь, цель оценки и подготовка документов.",
        "Пәтер сауалнамасы: мекенжай, аудан, бағалау мақсаты және құжаттарды дайындау.",
        "Apartment details, valuation purpose, inspection preference and document preparation.",
    ),
    "house": tr(
        "Сведения о доме и земельном участке для предварительного рассмотрения.",
        "Алдын ала қарау үшін үй мен жер учаскесі туралы мәліметтер.",
        "House and land details for an initial review.",
    ),
    "land": tr(
        "Данные об участке и правах на землю для согласования оценки.",
        "Бағалауды келісу үшін учаске мен жер құқықтары туралы деректер.",
        "Land parcel details and ownership information for appraisal planning.",
    ),
    "commercial": tr(
        "Анкета по нежилой недвижимости и перечень документов на объект.",
        "Тұрғын емес мүлік сауалнамасы және нысан құжаттарының тізімі.",
        "Commercial property details and a preliminary document checklist.",
    ),
    "accident": tr(
        "Описание автомобиля, обстоятельств и повреждений после ДТП.",
        "ЖКО-дан кейінгі автокөлік, оқиға және зақымдар сипаттамасы.",
        "Vehicle and accident damage details for review by the appraiser.",
    ),
    "damage": tr(
        "Информация об ущербе от залива, пожара или другого происшествия.",
        "Су басу, өрт немесе басқа оқиғадан болған залал туралы ақпарат.",
        "Details of property damage caused by water, fire or another incident.",
    ),
    "vehicle": tr(
        "Сведения об автомобиле и цели оценки его стоимости.",
        "Автокөлік және оның құнын бағалау мақсаты туралы мәліметтер.",
        "Vehicle information and the purpose of the valuation.",
    ),
    "equipment": tr(
        "Описание оборудования или спецтехники и доступных характеристик.",
        "Жабдық немесе арнайы техника және оның сипаттамалары.",
        "Equipment or machinery details and available specifications.",
    ),
    "inventory": tr(
        "Состав запасов и цель определения их стоимости.",
        "Қорлардың құрамы және олардың құнын анықтау мақсаты.",
        "Inventory description and the purpose of its valuation.",
    ),
    "business": tr(
        "Первичные сведения о бизнесе, долях или имущественном комплексе.",
        "Бизнес, үлестер немесе мүліктік кешен туралы бастапқы мәліметтер.",
        "Initial information about a business, shares or an enterprise asset complex.",
    ),
    "public": tr(
        "Описание задачи и требований органа, которому нужен отчёт.",
        "Міндет пен есепті қабылдайтын орган талаптарының сипаттамасы.",
        "Describe the request and the receiving authority's requirements.",
    ),
    "court": tr(
        "Материалы и вопросы для согласования возможности экспертной работы.",
        "Сараптама жүргізу мүмкіндігін келісуге арналған материалдар мен сұрақтар.",
        "Case details and questions to assess the scope and availability of expert work.",
    ),
    "intangible": tr(
        "Описание актива, прав на него и цели оценки.",
        "Активтің, оған құқықтардың және бағалау мақсатының сипаттамасы.",
        "Describe the intangible asset, relevant rights and valuation purpose.",
    ),
    "accounting": tr(
        "Сведения об активах для обсуждения задач учёта и переоценки.",
        "Есеп пен қайта бағалау міндеттерін талқылауға арналған актив деректері.",
        "Asset information for accounting and revaluation requirements.",
    ),
    "biological": tr(
        "Сведения о животных, их количестве и характеристиках.",
        "Жануарлар, олардың саны мен сипаттамалары туралы мәліметтер.",
        "Animal types, quantities and characteristics for valuation planning.",
    ),
    "custom": tr(
        "Опишите нестандартную задачу, чтобы оценщик уточнил порядок работы.",
        "Бағалаушы жұмыс тәртібін нақтылауы үшін ерекше міндетті сипаттаңыз.",
        "Describe a special request so the appraiser can clarify the next steps.",
    ),
}
