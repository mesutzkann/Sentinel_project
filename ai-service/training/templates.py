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
# Eight or so phrasings per language per intent, plus four in the mixed-language block. Each
# intent's comment says what separates it from the one it is most easily confused with -- which
# is what the model has to learn, and what a template blurring the two would teach it not to.
#
# The counts are not decoration: the split reserves one phrasing per intent for validation and
# two for test, so an intent-language pair with fewer than four phrasings can end up with none
# in training at all. That is exactly what happened to `mixed` in the first Phase 8 build.

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
    #
    # The tuned router scored 0.22 here and read twelve of these as LOG_QUERY, which is what
    # "show me a failed request" deserves: it opens the same way a request for log lines does.
    # The last three phrasings in each language name the artefact — trace, span, hop — because
    # that noun is the only thing separating this intent from "hand me the lines" and from
    # PERFORMANCE_ANALYSIS's "why is it slow".
    (Intent.TRACE_QUERY, "en"): (
        "show me a failed request through {service}",
        "pull a trace for {service} from the last {minutes} minutes",
        "follow one request end to end through {service}",
        "what does a single {service} call look like right now",
        "open a recent {service} request and show the steps",
        "give me the span breakdown for one {service} request",
        "I want the trace, not the log lines, for {service}",
        "how many hops does a {service} request make before it answers",
    ),
    (Intent.TRACE_QUERY, "tr"): (
        "{service} üzerinden geçen başarısız bir isteği göster",
        "{service} için son {minutes} dakikadan bir iz getir",
        "{service} üzerinden bir isteği baştan sona takip et",
        "{service}'a giden tek bir çağrı şu an neye benziyor",
        "{service}'ın son isteklerinden birini açıp adımları göster",
        "{service} için tek bir isteğin span dökümünü ver",
        "{service} için log satırı değil iz istiyorum",
        "bir {service} isteği cevap verene kadar kaç durak geçiyor",
    ),
    # PERFORMANCE_ANALYSIS: it is slow. Nothing said about errors.
    #
    # The line against TRACE_QUERY is the plan, not the vocabulary: "where is the time going" and
    # "the slowest spans" are `metrics-mcp/get_response_time` plus `traces-mcp/get_slowest_spans`,
    # which `plans.py` files under this intent — asking about *traffic*. TRACE_QUERY is one
    # request and its path, which is `get_recent_traces`. Six phrasings about where the time goes
    # sat under TRACE_QUERY until Phase 8 measured 0.13 here and twelve TRACE rows read as
    # LOG_QUERY; they were labelled against the table the planner acts on, so they moved here.
    (Intent.PERFORMANCE_ANALYSIS, "en"): (
        "why is {service} taking so long",
        "where does a checkout spend its time",
        "find the slowest spans in {service}",
        "which span is taking the time in {service}",
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
        "bir checkout zamanını nerede harcıyor",
        "{service} içindeki en yavaş span'leri bul",
        "{service}'ta zamanı hangi adım yiyor",
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
    # The mixed block: Turkish grammar carrying English technical nouns, which is how this team
    # actually types. **Four phrasings per intent, not one.** With one, the split's floor — a
    # phrasing to validation and two to test for every intent — reserved several intents' only
    # mixed template away from training, and the tuned router scored 0.11 on mixed against 0.90
    # on English. Four is the smallest number that leaves at least one in train for every intent
    # however the shuffle falls.
    (Intent.ERROR_ANALYSIS, "mixed"): (
        "{service}'ta {code} alıyoruz, exception ne",
        "{service} son {minutes} dakikada kaç tane {exception} throw etti",
        "{service}'ın error'larını type'a göre breakdown eder misin",
        "{service}'ta hâlâ {exception} var mı, count verir misin",
    ),
    (Intent.FULL_INVESTIGATION, "mixed"): (
        "{service} down gibi, investigate eder misin",
        "{service}'ta bir sorun var, root cause'u bulur musun",
        "{minutes} dakikadır {service} için alert geliyor, ne oluyor",
        "{service} incident'ında bana symptom değil cause lazım",
    ),
    (Intent.PERFORMANCE_ANALYSIS, "mixed"): (
        "{service} latency fırladı, bakar mısın",
        "{service} neden bu kadar yavaş, sebebini bul",
        "{service}'ın response time'ı niye bu kadar arttı",
        "{service} bu öğleden sonra slow'ladı, sürelerde ne değişti",
    ),
    (Intent.LOG_QUERY, "mixed"): (
        "{service} loglarını tail eder misin",
        "{service}'ın son {minutes} dakikalık log'larını dök",
        "{service} log'larında {exception} grep'ler misin",
        "{service} şu an ne yazıyor, satırları olduğu gibi ver",
    ),
    (Intent.DEPLOYMENT_CHECK, "mixed"): (
        "{service}'a deploy geçtik mi bugün",
        "{service}'ın son release'i ne zaman çıktı",
        "son {hours} saatte {service}'a bir change merge edildi mi",
        "{service} için son deployment'ın versiyonu ne",
    ),
    (Intent.DATABASE_HEALTH, "mixed"): (
        "{service} connection pool doldu mu acaba",
        "database tarafında lock ya da blocking var mı",
        "postgres'te {service} kaç connection tutuyor",
        "db healthy mi, bekleyen session var mı",
    ),
    (Intent.METRIC_QUERY, "mixed"): (
        "{service} cpu kaçta",
        "{service}'ın {percentile} değeri şu an ne",
        "{service} ne kadar memory kullanıyor",
        "{service} için son {minutes} dakikanın request rate'ini ver",
    ),
    (Intent.TRACE_QUERY, "mixed"): (
        "{service} için bir trace açar mısın",
        "{service}'ta tek bir request'in span'lerini listeler misin",
        "{service} üzerinden bir request'i end to end takip et",
        "{service}'ın failed request'lerinden birinin trace'ini göster",
    ),
    (Intent.CONFIG_LOOKUP, "mixed"): (
        "{service}'ın timeout config'i ne",
        "{service}'ta {setting} kaça set edilmiş",
        "{service} için current config'i göster",
        "{service}'ın env'inde {setting} ne durumda",
    ),
    (Intent.CODE_LOOKUP, "mixed"): (
        "{thing} hangi dosyada, kodu göster",
        "{thing} nerede implement edilmiş",
        "{service}'ta {thing}'in code'unu bulur musun",
        "{thing} hangi class'ta handle ediliyor",
    ),
    (Intent.HISTORICAL_SIMILARITY, "mixed"): (
        "bu issue daha önce oldu mu",
        "{service}'ta geçmişte benzer bir incident var mıydı",
        "aynı error'ı daha önce de görmüş müydük",
        "bu pattern history'de tekrar ediyor mu",
    ),
    (Intent.REMEDIATION_QUESTION, "mixed"): (
        "{service} için fix ne olmalı",
        "{service}'ı nasıl recover ederiz",
        "bunu mitigate etmek için ne yapmamız lazım",
        "{service} için rollback mı yapsak, öneri ver",
    ),
    (Intent.SERVICE_TOPOLOGY, "mixed"): (
        "{service} hangi servisleri call ediyor",
        "{service}'ın dependency'leri neler",
        "{service}'a kim request atıyor",
        "servisler arası call graph'ı çıkarır mısın",
    ),
    (Intent.KNOWLEDGE_QUESTION, "mixed"): (
        "deadlock için runbook var mı",
        "{service} için onboarding doc'u nerede",
        "connection pool tuning ile ilgili bir guide var mı",
        "incident response prosedürü nasıl işliyor, doc'u paylaş",
    ),
    (Intent.GENERAL_QUESTION, "mixed"): (
        "genel durum nasıl, her şey ok mi",
        "bugün status ne, özet geç",
        "bir update alabilir miyim",
        "şu an genel olarak her şey fine mı",
    ),
}


#: One flat tuple, which is what the generator walks.
ALL_TEMPLATES: tuple[Template, ...] = tuple(
    Template(intent, language, text)
    for (intent, language), texts in PHRASINGS.items()
    for text in texts
)
