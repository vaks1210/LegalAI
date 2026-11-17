"""
Respondent-facing languages.

Respondents choose (or are filed with) a language; WhatsApp/SMS notices,
the invite email's intro and the respondent portal follow it. Missing keys
fall back to English. Translations were prepared without native-speaker
legal review: have them reviewed before relying on them in production.
"""

LANGUAGES = {
    "en": {"name": "English",  "native": "English"},
    "hi": {"name": "Hindi",    "native": "हिन्दी"},
    "bn": {"name": "Bengali",  "native": "বাংলা"},
    "mr": {"name": "Marathi",  "native": "मराठी"},
    "ta": {"name": "Tamil",    "native": "தமிழ்"},
    "te": {"name": "Telugu",   "native": "తెలుగు"},
    "es": {"name": "Spanish",  "native": "Español"},
    "ar": {"name": "Arabic",   "native": "العربية", "rtl": True},
}
DEFAULT_LANGUAGE = "en"

_ALIASES = {"hindi": "hi", "bengali": "bn", "bangla": "bn", "marathi": "mr", "tamil": "ta",
            "telugu": "te", "spanish": "es", "espanol": "es", "español": "es", "arabic": "ar",
            "english": "en"}


def normalize_language(value: str | None) -> str | None:
    """'hi', 'hi-IN', 'Hindi' -> 'hi'. None for unsupported values."""
    if not value:
        return None
    v = str(value).strip().lower()
    v = _ALIASES.get(v, v.replace("_", "-").split("-")[0])
    return v if v in LANGUAGES else None


def case_language(case: dict) -> str:
    return normalize_language(case.get("respondent_language")) or DEFAULT_LANGUAGE


# ── Phone (WhatsApp/SMS) messages ────────────────────────────────────────
# Placeholders: {claimant} {ref} {amount} {date} {round} {n} {no} {reference}
MESSAGES = {
    "invite": {
        "en": "{claimant} has raised a dispute with you (case {ref}). You can resolve it online, without going to court. Please respond within 7 days.",
        "hi": "{claimant} ने आपके विरुद्ध एक विवाद दर्ज किया है (केस {ref})। आप इसे अदालत गए बिना ऑनलाइन सुलझा सकते हैं। कृपया 7 दिनों के भीतर जवाब दें।",
        "bn": "{claimant} আপনার বিরুদ্ধে একটি বিরোধ দায়ের করেছেন (কেস {ref})। আদালতে না গিয়েই আপনি এটি অনলাইনে মীমাংসা করতে পারেন। অনুগ্রহ করে 7 দিনের মধ্যে উত্তর দিন।",
        "mr": "{claimant} यांनी तुमच्याविरुद्ध एक वाद दाखल केला आहे (केस {ref}). न्यायालयात न जाता तुम्ही तो ऑनलाइन सोडवू शकता. कृपया 7 दिवसांत उत्तर द्या.",
        "ta": "{claimant} உங்களுக்கு எதிராக ஒரு தகராறை பதிவு செய்துள்ளார் (வழக்கு {ref}). நீதிமன்றம் செல்லாமல் இதை ஆன்லைனில் தீர்க்கலாம். 7 நாட்களுக்குள் பதிலளிக்கவும்.",
        "te": "{claimant} మీపై ఒక వివాదాన్ని నమోదు చేశారు (కేసు {ref}). కోర్టుకు వెళ్లకుండానే దీన్ని ఆన్‌లైన్‌లో పరిష్కరించుకోవచ్చు. దయచేసి 7 రోజుల్లో స్పందించండి.",
        "es": "{claimant} ha presentado una disputa contra usted (caso {ref}). Puede resolverla en línea, sin ir a los tribunales. Responda dentro de 7 días.",
        "ar": "قدّم {claimant} نزاعًا ضدك (القضية {ref}). يمكنك حله عبر الإنترنت دون اللجوء إلى المحكمة. يرجى الرد خلال 7 أيام.",
    },
    "invite_amount": {
        "en": "{claimant} has raised a dispute with you for {amount} (case {ref}). You can resolve it online, without going to court. Please respond within 7 days.",
        "hi": "{claimant} ने आपके विरुद्ध {amount} का विवाद दर्ज किया है (केस {ref})। आप इसे अदालत गए बिना ऑनलाइन सुलझा सकते हैं। कृपया 7 दिनों के भीतर जवाब दें।",
        "bn": "{claimant} আপনার বিরুদ্ধে {amount}-এর একটি বিরোধ দায়ের করেছেন (কেস {ref})। আদালতে না গিয়েই আপনি এটি অনলাইনে মীমাংসা করতে পারেন। অনুগ্রহ করে 7 দিনের মধ্যে উত্তর দিন।",
        "mr": "{claimant} यांनी तुमच्याविरुद्ध {amount} चा वाद दाखल केला आहे (केस {ref}). न्यायालयात न जाता तुम्ही तो ऑनलाइन सोडवू शकता. कृपया 7 दिवसांत उत्तर द्या.",
        "ta": "{claimant} உங்களுக்கு எதிராக {amount} தொகைக்கான தகராறை பதிவு செய்துள்ளார் (வழக்கு {ref}). நீதிமன்றம் செல்லாமல் இதை ஆன்லைனில் தீர்க்கலாம். 7 நாட்களுக்குள் பதிலளிக்கவும்.",
        "te": "{claimant} మీపై {amount} కోసం ఒక వివాదాన్ని నమోదు చేశారు (కేసు {ref}). కోర్టుకు వెళ్లకుండానే దీన్ని ఆన్‌లైన్‌లో పరిష్కరించుకోవచ్చు. దయచేసి 7 రోజుల్లో స్పందించండి.",
        "es": "{claimant} ha presentado una disputa contra usted por {amount} (caso {ref}). Puede resolverla en línea, sin ir a los tribunales. Responda dentro de 7 días.",
        "ar": "قدّم {claimant} نزاعًا ضدك بمبلغ {amount} (القضية {ref}). يمكنك حله عبر الإنترنت دون اللجوء إلى المحكمة. يرجى الرد خلال 7 أيام.",
    },
    "nudge": {
        "en": "Reminder: the response window for case {ref} with {claimant} closes on {date}. If you do not respond, the claimant may go to court with a record of your non-response.",
        "hi": "अनुस्मारक: {claimant} के साथ केस {ref} में जवाब देने की अवधि {date} को समाप्त हो रही है। जवाब न देने पर दावेदार आपके जवाब न देने के रिकॉर्ड के साथ अदालत जा सकता है।",
        "bn": "স্মরণ করিয়ে দেওয়া হচ্ছে: {claimant}-এর সঙ্গে কেস {ref}-এ উত্তর দেওয়ার সময়সীমা {date} তারিখে শেষ হবে। উত্তর না দিলে দাবিদার আপনার উত্তর না দেওয়ার রেকর্ডসহ আদালতে যেতে পারেন।",
        "mr": "स्मरणपत्र: {claimant} यांच्यासोबतच्या केस {ref} मध्ये उत्तर देण्याची मुदत {date} रोजी संपते. उत्तर न दिल्यास दावेदार तुमच्या प्रतिसाद न देण्याच्या नोंदीसह न्यायालयात जाऊ शकतात.",
        "ta": "நினைவூட்டல்: {claimant} உடனான வழக்கு {ref}-இல் பதிலளிக்கும் காலக்கெடு {date} அன்று முடிகிறது. பதிலளிக்காவிட்டால், நீங்கள் பதிலளிக்கவில்லை என்ற பதிவுடன் கோரிக்கையாளர் நீதிமன்றம் செல்லலாம்.",
        "te": "గుర్తు: {claimant}తో కేసు {ref}లో స్పందించే గడువు {date}న ముగుస్తుంది. మీరు స్పందించకపోతే, మీరు స్పందించలేదన్న రికార్డుతో దావాదారు కోర్టుకు వెళ్లవచ్చు.",
        "es": "Recordatorio: el plazo para responder en el caso {ref} con {claimant} vence el {date}. Si no responde, el reclamante puede acudir a los tribunales con constancia de su falta de respuesta.",
        "ar": "تذكير: تنتهي مهلة الرد في القضية {ref} مع {claimant} بتاريخ {date}. إذا لم ترد، فقد يلجأ المدعي إلى المحكمة مع سجل بعدم ردك.",
    },
    "proposal": {
        "en": "A settlement proposal has been issued in case {ref} (round {round}). Review it and accept or reject.",
        "hi": "केस {ref} में समझौते का प्रस्ताव जारी किया गया है (राउंड {round})। इसे देखें और स्वीकार या अस्वीकार करें।",
        "bn": "কেস {ref}-এ একটি মীমাংসা প্রস্তাব দেওয়া হয়েছে (রাউন্ড {round})। এটি দেখে গ্রহণ বা প্রত্যাখ্যান করুন।",
        "mr": "केस {ref} मध्ये तडजोडीचा प्रस्ताव जारी झाला आहे (फेरी {round}). तो पाहा आणि स्वीकारा किंवा नाकारा.",
        "ta": "வழக்கு {ref}-இல் சமரச முன்மொழிவு வெளியிடப்பட்டுள்ளது (சுற்று {round}). அதைப் பார்த்து ஏற்கவும் அல்லது நிராகரிக்கவும்.",
        "te": "కేసు {ref}లో రాజీ ప్రతిపాదన జారీ అయింది (రౌండ్ {round}). దాన్ని చూసి అంగీకరించండి లేదా తిరస్కరించండి.",
        "es": "Se ha emitido una propuesta de acuerdo en el caso {ref} (ronda {round}). Revísela y acéptela o recházela.",
        "ar": "صدر اقتراح تسوية في القضية {ref} (الجولة {round}). راجعه واقبله أو ارفضه.",
    },
    "proposal_amount": {
        "en": "A settlement proposal of {amount} has been issued in case {ref} (round {round}). Review it and accept or reject.",
        "hi": "केस {ref} में {amount} के समझौते का प्रस्ताव जारी किया गया है (राउंड {round})। इसे देखें और स्वीकार या अस्वीकार करें।",
        "bn": "কেস {ref}-এ {amount}-এর একটি মীমাংসা প্রস্তাব দেওয়া হয়েছে (রাউন্ড {round})। এটি দেখে গ্রহণ বা প্রত্যাখ্যান করুন।",
        "mr": "केस {ref} मध्ये {amount} चा तडजोडीचा प्रस्ताव जारी झाला आहे (फेरी {round}). तो पाहा आणि स्वीकारा किंवा नाकारा.",
        "ta": "வழக்கு {ref}-இல் {amount} சமரச முன்மொழிவு வெளியிடப்பட்டுள்ளது (சுற்று {round}). அதைப் பார்த்து ஏற்கவும் அல்லது நிராகரிக்கவும்.",
        "te": "కేసు {ref}లో {amount} రాజీ ప్రతిపాదన జారీ అయింది (రౌండ్ {round}). దాన్ని చూసి అంగీకరించండి లేదా తిరస్కరించండి.",
        "es": "Se ha emitido una propuesta de acuerdo de {amount} en el caso {ref} (ronda {round}). Revísela y acéptela o recházela.",
        "ar": "صدر اقتراح تسوية بمبلغ {amount} في القضية {ref} (الجولة {round}). راجعه واقبله أو ارفضه.",
    },
    "next_round": {
        "en": "Negotiation round {round} has opened in case {ref}. Submit your offer.",
        "hi": "केस {ref} में बातचीत का राउंड {round} शुरू हो गया है। अपना प्रस्ताव जमा करें।",
        "bn": "কেস {ref}-এ আলোচনার রাউন্ড {round} শুরু হয়েছে। আপনার প্রস্তাব জমা দিন।",
        "mr": "केस {ref} मध्ये वाटाघाटीची फेरी {round} सुरू झाली आहे. तुमचा प्रस्ताव सादर करा.",
        "ta": "வழக்கு {ref}-இல் பேச்சுவார்த்தை சுற்று {round} தொடங்கியுள்ளது. உங்கள் சலுகையைச் சமர்ப்பிக்கவும்.",
        "te": "కేసు {ref}లో చర్చల రౌండ్ {round} ప్రారంభమైంది. మీ ఆఫర్‌ను సమర్పించండి.",
        "es": "Se ha abierto la ronda de negociación {round} en el caso {ref}. Envíe su oferta.",
        "ar": "بدأت جولة التفاوض {round} في القضية {ref}. قدّم عرضك.",
    },
    "pay_single": {
        "en": "Settlement reached in case {ref}. Pay {amount} by {date}.",
        "hi": "केस {ref} में समझौता हो गया है। {date} तक {amount} का भुगतान करें।",
        "bn": "কেস {ref}-এ মীমাংসা হয়েছে। {date}-এর মধ্যে {amount} পরিশোধ করুন।",
        "mr": "केस {ref} मध्ये तडजोड झाली आहे. {date} पर्यंत {amount} भरा.",
        "ta": "வழக்கு {ref}-இல் சமரசம் ஏற்பட்டது. {date}-க்குள் {amount} செலுத்தவும்.",
        "te": "కేసు {ref}లో రాజీ కుదిరింది. {date} లోపు {amount} చెల్లించండి.",
        "es": "Acuerdo alcanzado en el caso {ref}. Pague {amount} antes del {date}.",
        "ar": "تم التوصل إلى تسوية في القضية {ref}. ادفع {amount} قبل {date}.",
    },
    "pay_plan": {
        "en": "Settlement reached in case {ref}: {n} instalments, the first of {amount} due on {date}.",
        "hi": "केस {ref} में समझौता हो गया है: {n} किस्तें, पहली किस्त {amount} की {date} को देय है।",
        "bn": "কেস {ref}-এ মীমাংসা হয়েছে: {n}টি কিস্তি, প্রথম কিস্তি {amount}, প্রদেয় {date}।",
        "mr": "केस {ref} मध्ये तडजोड झाली आहे: {n} हप्ते, पहिला हप्ता {amount} चा, {date} रोजी देय.",
        "ta": "வழக்கு {ref}-இல் சமரசம் ஏற்பட்டது: {n} தவணைகள், முதல் தவணை {amount}, {date} அன்று செலுத்த வேண்டும்.",
        "te": "కేసు {ref}లో రాజీ కుదిరింది: {n} వాయిదాలు, మొదటి వాయిదా {amount}, {date}న చెల్లించాలి.",
        "es": "Acuerdo alcanzado en el caso {ref}: {n} cuotas; la primera, de {amount}, vence el {date}.",
        "ar": "تم التوصل إلى تسوية في القضية {ref}: {n} أقساط، الأول بمبلغ {amount} مستحق في {date}.",
    },
    "quote_ref": {
        "en": "Quote reference {reference} when paying.",
        "hi": "भुगतान करते समय संदर्भ {reference} लिखें।",
        "bn": "পরিশোধের সময় রেফারেন্স {reference} উল্লেখ করুন।",
        "mr": "पैसे भरताना संदर्भ {reference} नमूद करा.",
        "ta": "செலுத்தும்போது குறிப்பு எண் {reference} குறிப்பிடவும்.",
        "te": "చెల్లించేటప్పుడు రిఫరెన్స్ {reference} పేర్కొనండి.",
        "es": "Indique la referencia {reference} al pagar.",
        "ar": "اذكر المرجع {reference} عند الدفع.",
    },
    "reminder_single": {
        "en": "Reminder: payment of {amount} for case {ref} is due on {date}.",
        "hi": "अनुस्मारक: केस {ref} के लिए {amount} का भुगतान {date} को देय है।",
        "bn": "স্মরণ করিয়ে দেওয়া হচ্ছে: কেস {ref}-এর জন্য {amount} পরিশোধের তারিখ {date}।",
        "mr": "स्मरणपत्र: केस {ref} साठी {amount} चा भरणा {date} रोजी देय आहे.",
        "ta": "நினைவூட்டல்: வழக்கு {ref}-க்கான {amount} செலுத்துதல் {date} அன்று உரியது.",
        "te": "గుర్తు: కేసు {ref} కోసం {amount} చెల్లింపు {date}న చెల్లించాలి.",
        "es": "Recordatorio: el pago de {amount} del caso {ref} vence el {date}.",
        "ar": "تذكير: دفعة بمبلغ {amount} للقضية {ref} مستحقة في {date}.",
    },
    "reminder_instalment": {
        "en": "Reminder: instalment {no} of {n} ({amount}) for case {ref} is due on {date}. Missing it by more than 7 days makes the full balance due.",
        "hi": "अनुस्मारक: केस {ref} की {n} में से किस्त {no} ({amount}) {date} को देय है। 7 दिन से अधिक की देरी होने पर पूरी बकाया राशि तुरंत देय हो जाएगी।",
        "bn": "স্মরণ করিয়ে দেওয়া হচ্ছে: কেস {ref}-এর {n}টির মধ্যে কিস্তি {no} ({amount}) {date} তারিখে প্রদেয়। 7 দিনের বেশি দেরি হলে সম্পূর্ণ বকেয়া অবিলম্বে প্রদেয় হবে।",
        "mr": "स्मरणपत्र: केस {ref} चा {n} पैकी हप्ता {no} ({amount}) {date} रोजी देय आहे. 7 दिवसांपेक्षा जास्त उशीर झाल्यास संपूर्ण थकबाकी त्वरित देय होईल.",
        "ta": "நினைவூட்டல்: வழக்கு {ref}-இன் {n} தவணைகளில் தவணை {no} ({amount}) {date} அன்று உரியது. 7 நாட்களுக்கு மேல் தாமதமானால் முழு நிலுவைத் தொகையும் உடனே செலுத்த வேண்டியதாகும்.",
        "te": "గుర్తు: కేసు {ref}లో {n} వాయిదాల్లో వాయిదా {no} ({amount}) {date}న చెల్లించాలి. 7 రోజులకు మించి ఆలస్యమైతే మొత్తం బకాయి వెంటనే చెల్లించాల్సి ఉంటుంది.",
        "es": "Recordatorio: la cuota {no} de {n} ({amount}) del caso {ref} vence el {date}. Un retraso de más de 7 días hace exigible todo el saldo.",
        "ar": "تذكير: القسط {no} من {n} ({amount}) للقضية {ref} مستحق في {date}. التأخر أكثر من 7 أيام يجعل كامل الرصيد مستحقًا فورًا.",
    },
    # Shown at the top of the (English) invite email for non-English respondents.
    "email_intro": {
        "en": "",
        "hi": "यह एक औपचारिक विवाद नोटिस है। नीचे दिए गए लिंक से केस हिन्दी में खोलें और 7 दिनों के भीतर जवाब दें।",
        "bn": "এটি একটি আনুষ্ঠানিক বিরোধ নোটিস। নিচের লিঙ্ক থেকে কেসটি বাংলায় খুলুন এবং 7 দিনের মধ্যে উত্তর দিন।",
        "mr": "ही एक औपचारिक वाद नोटीस आहे. खालील लिंकवरून केस मराठीत उघडा आणि 7 दिवसांत उत्तर द्या.",
        "ta": "இது ஒரு முறையான தகராறு அறிவிப்பு. கீழே உள்ள இணைப்பின் மூலம் வழக்கை தமிழில் திறந்து 7 நாட்களுக்குள் பதிலளிக்கவும்.",
        "te": "ఇది అధికారిక వివాద నోటీసు. కింది లింక్ ద్వారా కేసును తెలుగులో తెరిచి 7 రోజుల్లో స్పందించండి.",
        "es": "Este es un aviso formal de disputa. Abra el caso en español con el enlace de abajo y responda dentro de 7 días.",
        "ar": "هذا إشعار نزاع رسمي. افتح القضية باللغة العربية من الرابط أدناه ورد خلال 7 أيام.",
    },
}


def t(key: str, lang: str | None = None, **params) -> str:
    variants = MESSAGES.get(key) or {}
    text = variants.get(lang or DEFAULT_LANGUAGE) or variants.get(DEFAULT_LANGUAGE, key)
    try:
        return text.format(**params)
    except (KeyError, IndexError):
        return variants.get(DEFAULT_LANGUAGE, key).format(**params)
