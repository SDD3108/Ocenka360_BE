"""Version 1 stays immutable. New instructions require a new version and migration strategy."""

from apps.catalog.schema import evaluate, localize_document

GUIDE_VERSION = 1


def item(key, ru, kk, en, required=True):
    return {"key": key, "title": dict(ru=ru, kk=kk, en=en), "required": required}


HOME = [
    item("building", "Здание снаружи целиком", "Ғимараттың толық сыртқы көрінісі", "Whole building exterior"),
    item("entrance_outside", "Вход снаружи", "Кіреберістің сыртқы көрінісі", "Entrance from outside"),
    item("entrance_inside", "Подъезд внутри", "Кіреберістің ішкі көрінісі", "Entrance hall interior"),
    item("lift", "Лифт, если есть", "Лифт, бар болса", "Lift, if present", False),
    item("front_door", "Входная дверь", "Кіретін есік", "Front door"),
    item(
        "rooms",
        "Все жилые комнаты: несколько ракурсов каждой",
        "Әр тұрғын бөлме: бірнеше қырынан",
        "Every living room: multiple angles",
    ),
    item("kitchen", "Кухня", "Асүй", "Kitchen"),
    item("bathroom", "Санузлы", "Жуынатын бөлмелер", "Bathrooms"),
    item(
        "defects",
        "Дефекты и повреждения, если есть",
        "Ақаулар мен зақымдар, бар болса",
        "Defects and damage, if any",
        False,
    ),
]
HOUSE = [
    HOME[0],
    HOME[4],
    HOME[5],
    HOME[6],
    HOME[7],
    item("plot", "Участок и постройки", "Жер телімі мен құрылыстар", "Land and outbuildings"),
    item(
        "roof",
        "Крыша с безопасной точки на земле",
        "Шатыр: жердегі қауіпсіз нүктеден",
        "Roof from a safe point on the ground",
    ),
    HOME[-1],
]
VEHICLE = [
    item("front", "Автомобиль спереди", "Көліктің алдыңғы жағы", "Vehicle front"),
    item("rear", "Автомобиль сзади", "Көліктің артқы жағы", "Vehicle rear"),
    item("left", "Левая сторона", "Сол жағы", "Left side"),
    item("right", "Правая сторона", "Оң жағы", "Right side"),
    item("interior", "Салон и приборная панель", "Салон мен аспаптар панелі", "Interior and dashboard"),
    item(
        "vin",
        "VIN / номер кузова без посторонних документов",
        "VIN / шанақ нөмірі",
        "VIN / chassis number without unrelated documents",
    ),
    item(
        "damage",
        "Повреждения: общий и крупный план",
        "Зақымдар: жалпы және жақын көрініс",
        "Damage: overview and close-ups",
        False,
    ),
]
NOTICE = {
    "ru": "Снимайте при хорошем освещении, без фильтров, чётко и с нескольких ракурсов. Не рискуйте ради кадра. Эти подсказки не заменяют профессиональные стандарты: достаточность фотографий и возможность дистанционной оценки подтверждает оценщик.",
    "kk": "Жақсы жарықта, сүзгісіз, анық және бірнеше қырынан түсіріңіз. Сурет үшін қауіп төндірмеңіз. Бұл кеңестер кәсіби стандарттарды алмастырмайды: фотолардың жеткіліктілігі мен қашықтан бағалау мүмкіндігін бағалаушы растайды.",
    "en": "Use good lighting, no filters, sharp focus and multiple angles. Do not take risks for a photograph. These tips do not replace professional standards: the appraiser confirms whether the photos suffice and remote appraisal is possible.",
}


def requirements(order, language):
    documents = [
        *evaluate(order.form.definition, order.answers)["documents"],
        {
            "key": "additional",
            "title": {"ru": "Другие документы", "kk": "Басқа құжаттар", "en": "Other documents"},
            "help": {
                "ru": "Только относящиеся к заявке документы по просьбе оценщика.",
                "kk": "Бағалаушы сұраған өтінімге қатысты құжаттар ғана.",
                "en": "Only relevant documents requested by the appraiser.",
            },
            "required": False,
        },
    ]
    # The service determines the guide; mismatched asset cards cannot bypass required photos.
    slug = order.service_snapshot.get("slug", "")
    # Isolated developer demos have a generated slug but the same form as a real service.
    if slug.startswith("demo-"):
        slug = order.asset_snapshot.get("kind", "other")
    photos = (
        HOME
        if slug == "apartment"
        else HOUSE
        if slug == "house"
        else VEHICLE
        if slug in {"vehicle", "accident-damage"}
        else []
    )
    remote = order.answers.get("inspection_preference") == "remote"
    photo_list = []
    for photo in photos:
        required = photo["required"] or (slug == "accident-damage" and photo["key"] == "damage")
        photo_list.append(
            {"key": photo["key"], "title": photo["title"][language], "required": bool(remote and required)}
        )
    photo_list.append(
        {
            "key": "additional",
            "title": {"ru": "Дополнительные фотографии", "kk": "Қосымша фотолар", "en": "Additional photos"}[
                language
            ],
            "required": False,
        }
    )
    return {
        "guide_version": GUIDE_VERSION,
        "language": language,
        "documents": [localize_document(d, language) for d in documents],
        "photos": photo_list,
        "notice": NOTICE[language],
        "remote_requested": remote,
    }
