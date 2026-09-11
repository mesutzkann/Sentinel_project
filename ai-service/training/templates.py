"""The phrasings the routing dataset is generated from, written as questions rather than as rules.

**The one thing that would ruin this file is writing it from the rule router's keyword table.**
`routing/rule_router.py` matches `"slow quer"`, `"deadlock"`, `"connection pool"` and forty more;
a dataset built out of those terms would make the keyword baseline score close to perfect, and
Phase 8's whole claim — that a tuned 1.5B beats a keyword table on the questions people actually
ask — would be measured against a straw man. So these are written the way somebody asks at three
in the morning, and the overlap with the rule table is whatever it honestly is. A question about
logs usually does say "log"; a question about a service that fell over often says none of the
words the table looks for.

Each template is one phrasing with slots. The generator fills them across the five sample
services and the slot vocabularies below, which is what turns roughly ten phrasings per intent
per language into a hundred examples.

**Turkish is not translated English.** The Turkish templates were written as Turkish questions —
different word order, different idiom, and the code-switching that actually happens in a Turkish
engineering team ("payments'ta 500 alıyoruz"). A set of literal translations would teach the
model that Turkish is English with different tokens, which is the failure mode this bilingual
project cannot afford.
"""

from __future__ import annotations

from dataclasses import dataclass

from routing.schema import Intent


@dataclass(frozen=True, slots=True)
class Template:
    """One phrasing.

    ``text`` may contain ``{service}`` and the slot names in :data:`SLOTS`. A template with no
    ``{service}`` produces examples whose ``target_service`` is null, which is a label the model
    has to be able to produce: "which service calls which" names nobody on purpose.
    """

    intent: Intent
    language: str
    text: str

    @property
    def names_service(self) -> bool:
        return "{service}" in self.text


# What the slots can be filled with. Deliberately concrete: an incident question names a status
# code, a number of minutes, an exception. Generic filler would produce generic training data.
SLOTS: dict[str, tuple[str, ...]] = {
    "minutes": ("5", "15", "30", "60", "90"),
    "hours": ("2", "3", "6", "12"),
    "code": ("500", "502", "503", "504"),
    "exception": (
        "NullReferenceException",
        "NpgsqlException",
        "TaskCanceledException",
        "InvalidOperationException",
        "HttpRequestException",
    ),
    "setting": (
        "MaxPoolSize",
        "the connection string",
        "the request timeout",
        "the retry count",
        "the circuit breaker threshold",
    ),
    "thing": (
        "the checkout handler",
        "order creation",
        "payment authorisation",
        "the notification sender",
        "the health check",
    ),
    "percentile": ("p50", "p95", "p99"),
}

SERVICES: tuple[str, ...] = ("gateway", "users", "orders", "payments", "notifications")


# --------------------------------------------------------------------------- templates ----
#
# Roughly five phrasings per language per intent, plus a mixed-language block. Each intent's
# comment says what separates it from the one it is most easily confused with -- which is what
# the model has to learn, and what a template blurring the two would teach it not to.

PHRASINGS: dict[tuple[Intent, str], tuple[str, ...]] = {
    # FULL_INVESTIGATION: something is wrong and the asker wants the cause, not a signal.
    (Intent.FULL_INVESTIGATION, "en"): (
        "{service} is broken, work out what happened",
        "something is up with {service} and I have no idea what",
        "customers are complaining about {service}, look into it",
        "{service} started misbehaving about {minutes} minutes ago, why",
        "we are getting paged for {service}, what is going on",
        "the whole checkout flow is degraded, find the cause",
        "everything downstream of {service} is unhappy, get to the bottom of it",
        "{service} has been flapping since the {hours} o'clock deploy, why",
        "I need a cause for the {service} incident, not a symptom",
    ),
    (Intent.FULL_INVESTIGATION, "tr"): (
        "{service} bozuldu, ne olduğunu bul",
        "{service} tarafında bir tuhaflık var, sebebini çıkar",
        "müşteriler {service} için şikayet ediyor, bak bakalım",
        "{minutes} dakikadır {service} düzgün çalışmıyor, neden",
        "{service} için alarm aldık, durum ne",
        "{service} sabahtan beri inip kalkıyor, kökenine in",
        "{service} olayı için bana sebep lazım, belirti değil",
        "{service} yüzünden her yer etkilendi, ne olduğunu çöz",
    ),
    # ERROR_ANALYSIS: the failure is known; the question is what is throwing and how much.
    (Intent.ERROR_ANALYSIS, "en"): (
        "how many {exception} did {service} throw in the last {minutes} minutes",
        "{service} is returning {code}s, what is throwing",
        "what exceptions is {service} producing right now",
        "break down the failures in {service} by type",
        "is {service} still throwing {exception}",
        "what is blowing up in {service}",
        "count the failures coming out of {service} since lunch",
        "which exception dominates in {service} right now",
    ),
    (Intent.ERROR_ANALYSIS, "tr"): (
        "{service} son {minutes} dakikada kaç {exception} fırlattı",
        "{service} {code} dönüyor, ne patlıyor",
        "{service} şu an hangi istisnaları üretiyor",
        "{service} hatalarını tipine göre ayır",
        "{service}'ta {exception} hâlâ çıkıyor mu",
        "{service}'ta ne patlıyor",
        "öğleden beri {service}'tan çıkan hataları say",
        "{service}'ta şu an hangi istisna baskın",
    ),
    # LOG_QUERY: hand me the lines. No diagnosis asked for.
    (Intent.LOG_QUERY, "en"): (
        "show me the last {minutes} minutes of {service} logs",
        "what did {service} print around the restart",
        "grep {service} for {exception}",
        "pull up {service} output from the last {hours} hours",
        "any warnings in {service} lately",
        "tail {service} for me",
        "what is {service} writing out at the moment",
        "give me {service} lines containing timeout",
    ),
    (Intent.LOG_QUERY, "tr"): (
        "{service} son {minutes} dakikanın loglarını göster",
        "{service} yeniden başlarken ne yazdı",
        "{service} loglarında {exception} ara",
        "{service}'ın son {hours} saatlik çıktısını getir",
        "{service}'ta son zamanlarda uyarı var mı",
        "{service}'ı canlı izle",
        "{service} şu an ne yazıyor",
        "{service} satırlarından timeout geçenleri ver",
    ),
    # METRIC_QUERY: a number, asked for directly.
    (Intent.METRIC_QUERY, "en"): (
        "what is {service} sitting at for cpu",
        "how much memory is {service} using",
        "requests per second for {service} over the last {minutes} minutes",
        "graph the error rate of {service}",
        "what is the {percentile} for {service}",
        "give me the numbers for {service}",
        "how loaded is {service} right now",
        "what does the {percentile} look like for {service} over {hours} hours",
    ),
    (Intent.METRIC_QUERY, "tr"): (
        "{service} işlemci olarak nerede",
        "{service} ne kadar bellek kullanıyor",
        "{service} için son {minutes} dakikanın saniyelik istek sayısı",
        "{service} hata oranını çıkar",
        "{service} için {percentile} kaç",
        "{service} için sayıları ver",
        "{service} şu an ne kadar yüklü",
        "{service} için {hours} saatlik {percentile} nasıl görünüyor",
    ),
    # TRACE_QUERY: the shape of one request across services.
    (Intent.TRACE_QUERY, "en"): (
        "show me a failed request through {service}",
        "where does a checkout spend its time",
        "find the slowest spans in {service}",
        "pull a trace for {service} from the last {minutes} minutes",
        "which span is taking the time in {service}",
        "follow one request end to end through {service}",
        "what does a single {service} call look like right now",
        "open a recent {service} request and show the steps",
    ),
    (Intent.TRACE_QUERY, "tr"): (
        "{service} üzerinden geçen başarısız bir isteği göster",
        "bir checkout zamanını nerede harcıyor",
        "{service} içindeki en yavaş span'leri bul",
        "{service} için son {minutes} dakikadan bir iz getir",
        "{service}'ta zamanı hangi adım yiyor",
        "{service} üzerinden bir isteği baştan sona takip et",
        "{service}'a giden tek bir çağrı şu an neye benziyor",
        "{service}'ın son isteklerinden birini açıp adımları göster",
    ),
    # PERFORMANCE_ANALYSIS: it is slow. Nothing said about errors.
    (Intent.PERFORMANCE_ANALYSIS, "en"): (
        "why is {service} taking so long",
        "{service} got sluggish this afternoon, what changed in the timings",
        "requests to {service} are crawling, dig into it",
        "{service} used to answer instantly and now it does not",
        "where is the time going in {service}",
        "{service} is dragging, find out where",
        "we are seeing {service} take seconds instead of milliseconds",
        "what is slowing {service} down since this morning",
    ),
    (Intent.PERFORMANCE_ANALYSIS, "tr"): (
        "{service} neden bu kadar uzun sürüyor",
        "{service} öğleden sonra ağırlaştı, sürelerde ne değişti",
        "{service}'a giden istekler sürünüyor, araştır",
        "{service} eskiden anında dönüyordu, şimdi dönmüyor",
        "{service}'ta zaman nereye gidiyor",
        "{service} ağırlaşmış, nerede takıldığını bul",
        "{service} milisaniye yerine saniyelerle dönüyor",
        "sabahtan beri {service}'ı ne yavaşlatıyor",
    ),
    # DATABASE_HEALTH: the database as the subject, not as a dependency.
    (Intent.DATABASE_HEALTH, "en"): (
        "is the database healthy",
        "how many connections is {service} holding open",
        "are we blocking on anything in postgres",
        "which statements are eating the most total time",
        "check whether {service} is starved of connections",
        "is postgres coping",
        "how does the database look from {service}'s side",
        "are there sessions waiting on each other",
    ),
    (Intent.DATABASE_HEALTH, "tr"): (
        "veritabanı sağlıklı mı",
        "{service} kaç bağlantı açık tutuyor",
        "postgres tarafında bekleyen bir şey var mı",
        "toplam süreyi en çok hangi sorgular yiyor",
        "{service} bağlantı sıkıntısı çekiyor mu bak",
        "postgres yetişiyor mu",
        "{service} tarafından veritabanı nasıl görünüyor",
        "birbirini bekleyen oturumlar var mı",
    ),
    # DEPLOYMENT_CHECK: what changed, and when.
    (Intent.DEPLOYMENT_CHECK, "en"): (
        "did anything go out to {service} today",
        "what landed in {service} in the last {hours} hours",
        "was {service} touched before this started",
        "show me what shipped recently",
        "who merged something into {service} this morning",
        "what is the newest thing in {service}",
        "has anybody pushed to {service} since yesterday",
        "list what went live in the last {hours} hours",
    ),
    (Intent.DEPLOYMENT_CHECK, "tr"): (
        "bugün {service}'a bir şey çıktı mı",
        "son {hours} saatte {service}'a ne girdi",
        "bu başlamadan önce {service}'a dokunuldu mu",
        "son çıkanları göster",
        "bu sabah {service}'a kim bir şey birleştirdi",
        "{service}'taki en yeni şey ne",
        "dünden beri {service}'a bir şey gönderildi mi",
        "son {hours} saatte canlıya ne çıktı",
    ),
    # CODE_LOOKUP: where something is implemented.
    (Intent.CODE_LOOKUP, "en"): (
        "where is {thing} implemented",
        "show me how {service} handles retries",
        "which file does {thing} live in",
        "find the method that writes the order row",
        "open the part of {service} that talks to the database",
        "show me the source for {thing}",
        "which class is responsible for {thing}",
        "point me at the code behind {thing}",
    ),
    (Intent.CODE_LOOKUP, "tr"): (
        "{thing} nerede yazılmış",
        "{service} yeniden denemeyi nasıl yapıyor göster",
        "{thing} hangi dosyada",
        "sipariş satırını yazan metodu bul",
        "{service}'ın veritabanıyla konuşan kısmını aç",
        "{thing} kaynağını göster",
        "{thing} işinden hangi sınıf sorumlu",
        "{thing} arkasındaki kodu göster bana",
    ),
    # CONFIG_LOOKUP: what a value currently *is*, rather than where the code is.
    (Intent.CONFIG_LOOKUP, "en"): (
        "what is {setting} for {service}",
        "how is {setting} set in {service} right now",
        "did somebody change {setting}",
        "what value is {service} running with for {setting}",
        "check {service} for its environment variables",
        "what is {service} configured with for {setting}",
        "read me {setting} out of {service}",
        "confirm {setting} on {service}",
    ),
    (Intent.CONFIG_LOOKUP, "tr"): (
        "{service} için {setting} kaç",
        "{service}'ta {setting} şu an nasıl ayarlı",
        "{setting} değerini biri değiştirdi mi",
        "{service} {setting} için hangi değerle çalışıyor",
        "{service}'ın ortam değişkenlerine bak",
        "{service} {setting} için neyle yapılandırılmış",
        "{service}'tan {setting} değerini oku",
        "{service} üzerindeki {setting} ayarını doğrula",
    ),
    # SERVICE_TOPOLOGY: who calls whom. Usually names nobody, or names two.
    (Intent.SERVICE_TOPOLOGY, "en"): (
        "what does {service} talk to",
        "who calls {service}",
        "draw me the call graph",
        "is anything downstream of {service}",
        "which services sit between the edge and the database",
        "what breaks if {service} goes away",
        "map the services out for me",
        "does {service} depend on anything else",
    ),
    (Intent.SERVICE_TOPOLOGY, "tr"): (
        "{service} kiminle konuşuyor",
        "{service}'ı kim çağırıyor",
        "çağrı grafiğini çıkar",
        "{service}'ın altında ne var",
        "kenardan veritabanına kadar hangi servisler var",
        "{service} giderse ne kırılır",
        "servisleri bana haritala",
        "{service} başka bir şeye bağlı mı",
    ),
    # HISTORICAL_SIMILARITY: has this happened before.
    (Intent.HISTORICAL_SIMILARITY, "en"): (
        "have we hit this before",
        "did {service} do this last month too",
        "is there an old incident that looks like this",
        "what happened the last time {service} started timing out",
        "anything in the postmortems about {exception}",
        "we have seen something like this, when",
        "search the old incidents for {service}",
        "was there a similar outage on {service}",
    ),
    (Intent.HISTORICAL_SIMILARITY, "tr"): (
        "bunu daha önce yaşadık mı",
        "{service} geçen ay da böyle yapmış mıydı",
        "buna benzeyen eski bir olay var mı",
        "{service} en son zaman aşımına düştüğünde ne olmuştu",
        "postmortem'lerde {exception} ile ilgili bir şey var mı",
        "buna benzer bir şey görmüştük, ne zamandı",
        "eski olaylarda {service} ara",
        "{service}'ta benzer bir kesinti olmuş muydu",
    ),
    # KNOWLEDGE_QUESTION: a documentation question, with no live system in it.
    (Intent.KNOWLEDGE_QUESTION, "en"): (
        "how do you normally diagnose a deadlock",
        "what does the runbook say about pool exhaustion",
        "explain what a circuit breaker is supposed to do here",
        "is there a procedure written down for this",
        "what is {service} for, architecturally",
        "what is the documented way to handle this",
        "read me the notes on connection pooling",
        "how is {service} supposed to behave",
    ),
    (Intent.KNOWLEDGE_QUESTION, "tr"): (
        "kilitlenme normalde nasıl teşhis edilir",
        "runbook havuz tükenmesi için ne diyor",
        "buradaki devre kesici ne işe yarıyor anlat",
        "bunun yazılı bir prosedürü var mı",
        "{service} mimari olarak ne işe yarıyor",
        "bunun yazılı yöntemi nedir",
        "bağlantı havuzu notlarını oku",
        "{service} nasıl davranmalı",
    ),
    # REMEDIATION_QUESTION: what should be done about it.
    (Intent.REMEDIATION_QUESTION, "en"): (
        "how do I get {service} back",
        "what is the safest way to clear this",
        "should I restart {service} or wait",
        "give me the steps to recover from this",
        "what would you do about {exception} here",
        "what do we do now",
        "talk me through fixing {service}",
        "is there anything safe I can do right now",
    ),
    (Intent.REMEDIATION_QUESTION, "tr"): (
        "{service}'ı nasıl geri getiririm",
        "bunu temizlemenin en güvenli yolu ne",
        "{service}'ı yeniden başlatayım mı bekleyeyim mi",
        "bundan çıkmak için adımları ver",
        "buradaki {exception} için ne yapardın",
        "şimdi ne yapıyoruz",
        "{service}'ı düzeltmeyi anlat",
        "şu an güvenle yapabileceğim bir şey var mı",
    ),
    (Intent.GENERAL_QUESTION, "en"): (
        "how are things looking",
        "anything I should know about",
        "is everything alright",
        "give me a summary",
        "what should I be looking at",
        "status of {service}",
        "quick update please",
        "anything on fire",
        "what is the picture this morning",
    ),
    (Intent.GENERAL_QUESTION, "tr"): (
        "durum nasıl",
        "bilmem gereken bir şey var mı",
        "her şey yolunda mı",
        "bana bir özet çıkar",
        "neye bakmam lazım",
        "{service} durumu",
        "kısa bir güncelleme ver",
        "yanan bir yer var mı",
        "bu sabahki tablo ne",
    ),
    (Intent.ERROR_ANALYSIS, "mixed"): (
        "{service}'ta {code} alıyoruz, exception ne",
    ),
    (Intent.FULL_INVESTIGATION, "mixed"): (
        "{service} down gibi, investigate eder misin",
    ),
    (Intent.PERFORMANCE_ANALYSIS, "mixed"): (
        "{service} latency fırladı, bakar mısın",
    ),
    (Intent.LOG_QUERY, "mixed"): (
        "{service} loglarını tail eder misin",
    ),
    (Intent.DEPLOYMENT_CHECK, "mixed"): (
        "{service}'a deploy geçtik mi bugün",
    ),
    (Intent.DATABASE_HEALTH, "mixed"): (
        "{service} connection pool doldu mu acaba",
    ),
    (Intent.METRIC_QUERY, "mixed"): (
        "{service} cpu kaçta",
    ),
    (Intent.TRACE_QUERY, "mixed"): (
        "{service} için bir trace açar mısın",
    ),
    (Intent.CONFIG_LOOKUP, "mixed"): (
        "{service}'ın timeout config'i ne",
    ),
    (Intent.CODE_LOOKUP, "mixed"): (
        "{thing} hangi dosyada, kodu göster",
    ),
    (Intent.HISTORICAL_SIMILARITY, "mixed"): (
        "bu issue daha önce oldu mu",
    ),
    (Intent.REMEDIATION_QUESTION, "mixed"): (
        "{service} için fix ne olmalı",
    ),
    (Intent.SERVICE_TOPOLOGY, "mixed"): (
        "{service} hangi servisleri call ediyor",
    ),
    (Intent.KNOWLEDGE_QUESTION, "mixed"): (
        "deadlock için runbook var mı",
    ),
    (Intent.GENERAL_QUESTION, "mixed"): (
        "genel durum nasıl, her şey ok mi",
    ),
}


#: One flat tuple, which is what the generator walks.
ALL_TEMPLATES: tuple[Template, ...] = tuple(
    Template(intent, language, text)
    for (intent, language), texts in PHRASINGS.items()
    for text in texts
)
