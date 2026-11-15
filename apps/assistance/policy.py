import re

from django.conf import settings

POLICY_VERSION = "gemini-assistance-v1"
PROMPT_VERSION = "ocenka-assist-v1"
NOTICE = {
    "ru": "Gemini — внешний сервис Google. Ему передаются ваш вопрос, ограниченный контекст услуги/анкеты и только явно выбранные фотографии. Не вводите ИИН, ФИО, контакты, точные адреса и данные документов. Перед отправкой фото скройте лица, номера автомобилей, VIN и документы. Автоматическое обезличивание не гарантируется. Возможна обработка за пределами Казахстана; удаление данных в приложении не отзывает уже переданное Google. AI может ошибаться: это подсказка, а не официальный отчёт, проверка подлинности или окончательная оценка. Использование добровольное; можно работать без AI.",
    "kk": "Gemini — Google сыртқы қызметі. Оған сұрағыңыз, қызмет/сауалнама туралы шектеулі мәлімет және тек өзіңіз таңдаған фотолар жіберіледі. ЖСН, аты-жөн, байланыс, нақты мекенжай мен құжат деректерін енгізбеңіз. Фото жібермес бұрын беттерді, көлік нөмірін, VIN мен құжаттарды жасырыңыз. Автоматты иесіздендіруге кепілдік жоқ. Өңдеу Қазақстаннан тыс болуы мүмкін; қолданбадағы жою Google-ға жіберілген деректі қайтармайды. AI қателесуі мүмкін: бұл кеңес, ресми есеп не түпкілікті бағалау емес. AI қолдану ерікті.",
    "en": "Gemini is an external Google service. It receives your question, limited service/form context and only explicitly selected photos. Do not enter identity numbers, names, contacts, exact addresses or document data. Mask faces, plates, VINs and documents before sending photos. Automatic anonymization is not guaranteed. Processing may occur outside Kazakhstan; deleting app data cannot recall data already sent to Google. AI may be wrong: its output is guidance, not an official report, authenticity check or final appraisal. AI is optional.",
}
DISCLAIMER = {
    "ru": "Предварительная подсказка AI. Проверку материалов, выводы и окончательное решение выполняет оценщик.",
    "kk": "AI алдын ала кеңесі. Материалдарды тексеруді, қорытынды мен соңғы шешімді бағалаушы жасайды.",
    "en": "Preliminary AI guidance. The appraiser verifies materials and makes all final decisions.",
}


def availability():
    # Operational acknowledgements are not proof of a compliant contract or valid key.
    if not settings.GEMINI_ENABLED:
        return "disabled"
    if not settings.GEMINI_API_KEY or not settings.GEMINI_MODEL:
        return "credentials_missing"
    if not re.fullmatch(r"gemini-[a-z0-9][a-z0-9.-]{1,90}", settings.GEMINI_MODEL):
        return "invalid_model"
    if not settings.GEMINI_PAID_PROJECT_CONFIRMED or not settings.GEMINI_DATA_PROCESSING_APPROVED:
        return "data_processing_not_configured"
    return "configured"


def enabled():
    return availability() == "configured"
