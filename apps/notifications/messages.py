# Only server-owned generic text is allowed in push. Never interpolate comment/PII.
TEXT = {
    "new_order": ("Поступила новая заявка", "Жаңа өтінім келді", "A new order is available"),
    "comment_added": ("Новый комментарий к заявке", "Өтінімге жаңа пікір қосылды", "New order comment"),
    "materials_requested": (
        "Оценщик запросил дополнительные материалы",
        "Бағалаушы қосымша материалдар сұрады",
        "The appraiser requested more information",
    ),
    "resubmitted": (
        "Клиент отправил уточнения",
        "Клиент қосымша ақпарат жіберді",
        "The client submitted additional information",
    ),
    "material_request_accept": (
        "Дополнительные материалы приняты",
        "Қосымша материалдар қабылданды",
        "Additional information accepted",
    ),
    "material_request_return": (
        "Нужно дополнить материалы",
        "Материалдарды толықтыру қажет",
        "Additional information needs correction",
    ),
    "material_request_cancel": (
        "Запрос материалов закрыт",
        "Материалдар сұрауы жабылды",
        "Information request closed",
    ),
    "accept": ("Оценщик принял заявку", "Бағалаушы өтінімді қабылдады", "The appraiser accepted your order"),
    "request_information": (
        "Оценщик запросил уточнения",
        "Бағалаушы нақтылау сұрады",
        "The appraiser requested clarification",
    ),
    "price_offered": (
        "Оценщик предложил стоимость услуги",
        "Бағалаушы қызмет құнын ұсынды",
        "A price proposal is ready",
    ),
    "price_accept": (
        "Клиент согласовал стоимость",
        "Клиент бағаны мақұлдады",
        "The client accepted the price",
    ),
    "payment_recorded": (
        "Поступление оплаты подтверждено",
        "Төлемнің түскені расталды",
        "Payment received and confirmed",
    ),
    "refund_recorded": ("Возврат оплаты подтверждён", "Төлемнің қайтарылғаны расталды", "Refund confirmed"),
    "inspection_confirmed": ("Осмотр согласован", "Тексеру уақыты келісілді", "Inspection confirmed"),
    "inspection_offered": (
        "Оценщик предложил время осмотра",
        "Бағалаушы тексеру уақытын ұсынды",
        "The appraiser proposed an inspection time",
    ),
    "inspection_requested": (
        "Клиент предложил время осмотра",
        "Клиент тексеру уақытын ұсынды",
        "The client proposed an inspection time",
    ),
    "updated": ("В заявке есть обновление", "Өтінім жаңартылды", "There is an update to your order"),
}
ACTIONS = {
    "comment_added",
    "materials_requested",
    "resubmitted",
    "material_request_accept",
    "material_request_return",
    "material_request_cancel",
    "start_review",
    "accept",
    "reject",
    "cancelled",
    "request_information",
    "price_offered",
    "price_accept",
    "price_decline",
    "price_withdraw",
    "payment_recorded",
    "payment_simulated",
    "refund_recorded",
    "cancellation_requested",
    "cancellation_offered",
    "cancellation_accept",
    "cancellation_withdraw",
    "inspection_requested",
    "inspection_offered",
    "inspection_confirmed",
    "inspection_cancelled",
    "inspection_started",
    "inspection_completed",
    "material_rejected",
}


def body(kind, language):
    return TEXT.get(kind, TEXT["updated"])[{"ru": 0, "kk": 1, "en": 2}.get(language, 0)]
