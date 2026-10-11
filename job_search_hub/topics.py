"""
Controlled topic vocabulary for Job Search Hub.

Only canonical values defined here may be written to Notion. The vocabulary
must remain below Notion's multi-select option and schema-size limits.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

# ---------------------------------------------------------------------------
# Canonical vocabulary: exactly 99 topics
# ---------------------------------------------------------------------------

TOPICS_BY_CATEGORY: dict[str, tuple[str, ...]] = {
    "Programming Languages": (
        "Python",
        "JavaScript",
        "TypeScript",
        "Java",
        "C#",
        "C++",
        "Go",
        "Rust",
        "JOBHUB_SQL",
    ),
    "Frontend and Mobile": (
        "Frontend Development",
        "Web Development",
        "React",
        "Next.js",
        "Vue",
        "Angular",
        "JOBHUB_HTML",
        "JOBHUB_CSS",
        "Accessibility",
        "Mobile Development",
        "React Native",
        "iOS",
        "Android",
    ),
    "Backend and APIs": (
        "Backend Development",
        "Node.js",
        "Django",
        "FastAPI",
        "Ruby on Rails",
        ".NET",
        "REST APIs",
        "GraphQL",
        "API Integration",
        "Authentication",
        "Microservices",
    ),
    "Data, Databases, and AI": (
        "Data Engineering",
        "Data Science",
        "Data Analysis",
        "Business Intelligence",
        "Data Modeling",
        "Data Pipelines",
        "JOBHUB_ETL",
        "Data Warehousing",
        "PostgreSQL",
        "SQL Server",
        "MongoDB",
        "Snowflake",
        "Databricks",
        "Power BI",
        "Artificial Intelligence",
        "Machine Learning",
        "Generative AI",
        "Large Language Models",
        "Computer Vision",
        "MLOps",
        "PyTorch",
        "TensorFlow",
    ),
    "Cloud, Infrastructure, and Delivery": (
        "Amazon Web Services",
        "Microsoft Azure",
        "Google Cloud Platform",
        "Docker",
        "Kubernetes",
        "Terraform",
        "Infrastructure as Code",
        "Linux",
        "Distributed Systems",
        "Reliability Engineering",
        "Observability",
        "DevOps",
        "CI/CD",
    ),
    "Testing and Security": (
        "Software Testing",
        "Test Automation",
        "Unit Testing",
        "Integration Testing",
        "End-to-End Testing",
        "Quality Assurance",
        "Code Review",
        "Debugging",
        "Security",
        "Identity and Access Management",
    ),
    "Engineering and Delivery": (
        "Software Engineering",
        "Full Stack Development",
        "System Design",
        "Software Architecture",
        "Platform Engineering",
        "Automation",
        "Deployment",
        "Performance Optimization",
    ),
    "Employment": (
        "Years of Experience",
        "Remote",
        "Hybrid",
        "On-site",
        "Employment Sponsorship",
        "Contract",
        "Travel",
        "Relocation Assistance",
    ),
    "Geography and Other": (
        "United States",
        "Canada",
        "United Kingdom",
        "Europe",
        "Other",
    ),
}

CANONICAL_TOPICS: tuple[str, ...] = tuple(
    topic for category_topics in TOPICS_BY_CATEGORY.values() for topic in category_topics
)
CANONICAL_TOPIC_SET: frozenset[str] = frozenset(CANONICAL_TOPICS)

# Notion permits at most 100 options in a multi-select schema. Keep one slot
# in reserve so a manual repair remains possible without first editing code.
MAX_NOTION_SCHEMA_OPTIONS = 100
MAX_CANONICAL_TOPIC_OPTIONS = MAX_NOTION_SCHEMA_OPTIONS - 1

# The API recommends keeping an entire data-source schema below 50 KB. This
# conservative local cap covers the topic names and the JSON option-object
# overhead, before the rest of the table's schema is included.
MAX_CANONICAL_TOPIC_SCHEMA_BYTES = 16_000

# These are the only multi-select topic fields that automation may write.
# The three Opportunity fields are legacy mirrors; keeping them separate makes
# it impossible to accidentally treat them as an active controlled vocabulary.
ACTIVE_NOTION_TOPIC_PROPERTIES: frozenset[tuple[str, str]] = frozenset(
    {
        ("answer_bank", "Topics"),
        ("qualification_requirements", "Topics"),
    }
)
LEGACY_OPPORTUNITY_TOPIC_PROPERTIES: tuple[str, ...] = (
    "Qualification Topics",
    "Hard Qualification Topics",
    "Preferred Qualification Topics",
)

if len(CANONICAL_TOPICS) > MAX_CANONICAL_TOPIC_OPTIONS:
    raise RuntimeError(
        "Canonical topic count exceeds Notion's "
        f"{MAX_CANONICAL_TOPIC_OPTIONS}-option safety limit: "
        f"{len(CANONICAL_TOPICS)}"
    )

if len(CANONICAL_TOPICS) != len(CANONICAL_TOPIC_SET):
    raise RuntimeError("Duplicate canonical topics detected.")

_canonical_topic_schema_bytes = sum(len(topic.encode("utf-8")) + 48 for topic in CANONICAL_TOPICS)
if _canonical_topic_schema_bytes > MAX_CANONICAL_TOPIC_SCHEMA_BYTES:
    raise RuntimeError(
        "Canonical topic option schema is too large for the configured safety limit: "
        f"{_canonical_topic_schema_bytes} bytes"
    )


# ---------------------------------------------------------------------------
# Text normalization
# ---------------------------------------------------------------------------


def _normalized_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.casefold()
    value = value.replace("&", " and ")
    value = value.replace("’", "'")
    value = re.sub(r"[/_|]+", " ", value)
    value = re.sub(r"[^a-z0-9+#.\-']+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    phrase = _normalized_key(phrase)
    escaped = re.escape(phrase).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![a-z0-9+#]){escaped}(?![a-z0-9+#])")


# ---------------------------------------------------------------------------
# Exact aliases
# ---------------------------------------------------------------------------

TopicAliasValue = str | tuple[str, ...]

ALIASES: dict[str, TopicAliasValue] = {
    # Languages
    "js": "JavaScript",
    "ecmascript": "JavaScript",
    "es5": "JavaScript",
    "es6": "JavaScript",
    "ts": "TypeScript",
    "golang": "Go",
    "c sharp": "C#",
    "c-sharp": "C#",
    "cplusplus": "C++",
    # Frontend and mobile
    "reactjs": "React",
    "react js": "React",
    "react.js": "React",
    "nextjs": "Next.js",
    "next js": "Next.js",
    "vuejs": "Vue",
    "vue js": "Vue",
    "vue.js": "Vue",
    "angularjs": "Angular",
    "html5": "JOBHUB_HTML",
    "css3": "JOBHUB_CSS",
    "front end development": "Frontend Development",
    "front-end development": "Frontend Development",
    "frontend engineering": "Frontend Development",
    "front end engineering": "Frontend Development",
    "web application development": "Web Development",
    "web accessibility": "Accessibility",
    "a11y": "Accessibility",
    "mobile app development": "Mobile Development",
    "mobile application development": "Mobile Development",
    "ios development": "iOS",
    "iphone development": "iOS",
    "ipad development": "iOS",
    "swift": "iOS",
    "swiftui": "iOS",
    "uikit": "iOS",
    "objective-c": "iOS",
    "objective c": "iOS",
    "android development": "Android",
    "kotlin": "Android",
    "flutter": "Mobile Development",
    # Backend and APIs
    "node": "Node.js",
    "nodejs": "Node.js",
    "node js": "Node.js",
    "rails": "Ruby on Rails",
    "asp.net": ".NET",
    "asp.net core": ".NET",
    "dotnet": ".NET",
    ".net core": ".NET",
    "rest": "REST APIs",
    "rest api": "REST APIs",
    "restful api": "REST APIs",
    "api development": "API Integration",
    "api design": "REST APIs",
    "graphql api": "GraphQL",
    "oauth": "Authentication",
    "oauth2": "Authentication",
    "openid": "Authentication",
    "openid connect": "Authentication",
    "microservice": "Microservices",
    "microservices architecture": "Microservices",
    # Data and analytics
    "bi": "Business Intelligence",
    "business intelligence development": "Business Intelligence",
    "analytics engineering": "Data Engineering",
    "data pipeline": "Data Pipelines",
    "data ingestion": "Data Pipelines",
    "data warehouse": "Data Warehousing",
    "extract transform load": "JOBHUB_ETL",
    "elt": "JOBHUB_ETL",
    "tableau": "Business Intelligence",
    "powerbi": "Power BI",
    "power bi": "Power BI",
    "snowflake": "Snowflake",
    "databricks": "Databricks",
    "apache spark": "Databricks",
    "spark": "Databricks",
    "apache airflow": "Data Pipelines",
    "airflow": "Data Pipelines",
    "apache kafka": "Data Pipelines",
    "kafka": "Data Pipelines",
    "postgres": "PostgreSQL",
    "mssql": "SQL Server",
    "ms sql server": "SQL Server",
    "mysql": "JOBHUB_SQL",
    "sqlite": "JOBHUB_SQL",
    "oracle database": "JOBHUB_SQL",
    "mongo": "MongoDB",
    "mongo db": "MongoDB",
    "database design": "Data Modeling",
    # AI and ML
    "ai": "Artificial Intelligence",
    "ai engineering": "Artificial Intelligence",
    "ai engineer": "Artificial Intelligence",
    "ml": "Machine Learning",
    "machine learning engineering": "Machine Learning",
    "genai": "Generative AI",
    "gen ai": "Generative AI",
    "llm": "Large Language Models",
    "llms": "Large Language Models",
    "large language model": "Large Language Models",
    "ai agents": "Large Language Models",
    "ai agent": "Large Language Models",
    "agentic ai": "Large Language Models",
    "prompt engineering": "Large Language Models",
    "openai": "Large Language Models",
    "openai api": "Large Language Models",
    "nlp": "Machine Learning",
    "natural language processing": "Machine Learning",
    "vision ai": "Computer Vision",
    "deep learning": "Machine Learning",
    "computer vision": "Computer Vision",
    "ml ops": "MLOps",
    "machine learning operations": "MLOps",
    "pytorch": "PyTorch",
    "tensorflow": "TensorFlow",
    # Cloud and delivery
    "aws": "Amazon Web Services",
    "amazon aws": "Amazon Web Services",
    "azure": "Microsoft Azure",
    "gcp": "Google Cloud Platform",
    "google cloud": "Google Cloud Platform",
    "k8s": "Kubernetes",
    "iac": "Infrastructure as Code",
    "infrastructure-as-code": "Infrastructure as Code",
    "site reliability engineering": "Reliability Engineering",
    "sre": "Reliability Engineering",
    "monitoring": "Observability",
    "system monitoring": "Observability",
    "continuous integration": "CI/CD",
    "continuous delivery": "CI/CD",
    "ci cd": "CI/CD",
    "github actions": "CI/CD",
    "jenkins": "CI/CD",
    "production deployment": "Deployment",
    "release management": "Deployment",
    "workflow automation": "Automation",
    "workflow orchestration": "Automation",
    "configuration management": "Automation",
    "system optimization": "Performance Optimization",
    "cost optimization": "Performance Optimization",
    "scalability": "Performance Optimization",
    "high availability": "Reliability Engineering",
    # Testing
    "testing": "Software Testing",
    "qa": "Quality Assurance",
    "qa automation": "Test Automation",
    "automated testing": "Test Automation",
    "test automation framework": "Test Automation",
    "api testing": "Integration Testing",
    "database testing": "Integration Testing",
    "e2e testing": "End-to-End Testing",
    "playwright": "End-to-End Testing",
    "cypress": "End-to-End Testing",
    "selenium": "End-to-End Testing",
    "postman": "Integration Testing",
    # Security
    "application security": "Security",
    "cloud security": "Security",
    "data security": "Security",
    "security engineering": "Security",
    "security clearance": "Security",
    "iam": "Identity and Access Management",
    # Engineering
    "fullstack development": "Full Stack Development",
    "full-stack development": "Full Stack Development",
    "full stack engineering": "Full Stack Development",
    "system architecture": "Software Architecture",
    "architecture": "Software Architecture",
    "systems engineering": "System Design",
    "software development": "Software Engineering",
    "software engineer": "Software Engineering",
    "developer experience": "Platform Engineering",
    "prototyping": "Software Engineering",
    "robotics": "Software Engineering",
    # Employment
    "minimum years experience": "Years of Experience",
    "minimum years of experience": "Years of Experience",
    "years experience": "Years of Experience",
    "year of experience": "Years of Experience",
    "equivalent work experience": "Years of Experience",
    "remote work": "Remote",
    "fully remote": "Remote",
    "hybrid work": "Hybrid",
    "on site": "On-site",
    "onsite": "On-site",
    "visa sponsorship": "Employment Sponsorship",
    "immigration sponsorship": "Employment Sponsorship",
    "work visa": "Employment Sponsorship",
    "w2": "Contract",
    "1099": "Contract",
    "contract-to-hire": "Contract",
    "contract to hire": "Contract",
    "temporary": "Contract",
    "travel required": "Travel",
    "relocation": "Relocation Assistance",
    "relocation support": "Relocation Assistance",
    # Geography
    "us": "United States",
    "usa": "United States",
    "u s": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom",
    "great britain": "United Kingdom",
    "england": "United Kingdom",
}


def _build_normalized_aliases() -> dict[str, tuple[str, ...]]:
    normalized: dict[str, tuple[str, ...]] = {}

    for alias, canonical_value in ALIASES.items():
        values = (canonical_value,) if isinstance(canonical_value, str) else canonical_value

        retained = tuple(topic for topic in values if topic in CANONICAL_TOPIC_SET)
        if retained:
            normalized[_normalized_key(alias)] = retained

    return normalized


_NORMALIZED_ALIASES = _build_normalized_aliases()


# ---------------------------------------------------------------------------
# Keyword rules
# ---------------------------------------------------------------------------

KEYWORD_RULES: tuple[
    tuple[tuple[str, ...], tuple[str, ...]],
    ...,
] = (
    # Languages
    (("typescript",), ("TypeScript",)),
    (("javascript", "ecmascript"), ("JavaScript",)),
    (("python",), ("Python",)),
    (("java",), ("Java",)),
    (("golang",), ("Go",)),
    (("rust",), ("Rust",)),
    (("c++",), ("C++",)),
    (("c#", "c sharp"), ("C#",)),
    (("sql",), ("JOBHUB_SQL",)),
    # Frontend and mobile
    (("react native",), ("React Native",)),
    (("next.js", "nextjs", "next js"), ("Next.js",)),
    (("react",), ("React",)),
    (("angular",), ("Angular",)),
    (("vue.js", "vuejs", "vue"), ("Vue",)),
    (("html",), ("JOBHUB_HTML",)),
    (("css",), ("JOBHUB_CSS",)),
    (("accessibility", "a11y"), ("Accessibility",)),
    (("frontend", "front-end", "front end"), ("Frontend Development",)),
    (("web development", "web application"), ("Web Development",)),
    (("react native",), ("React Native",)),
    (("ios", "swiftui", "uikit", "objective-c"), ("iOS",)),
    (("android", "kotlin"), ("Android",)),
    (
        ("mobile development", "mobile application", "flutter"),
        ("Mobile Development",),
    ),
    # Backend and APIs
    (("backend", "back-end", "back end"), ("Backend Development",)),
    (("node.js", "nodejs", "node js"), ("Node.js",)),
    (("django",), ("Django",)),
    (("fastapi",), ("FastAPI",)),
    (("ruby on rails",), ("Ruby on Rails",)),
    (("asp.net", "dotnet", ".net"), (".NET",)),
    (("graphql",), ("GraphQL",)),
    (("oauth", "openid", "authentication"), ("Authentication",)),
    (("api integration", "integrate api"), ("API Integration",)),
    (("rest api", "restful api", "api design"), ("REST APIs",)),
    (("microservice",), ("Microservices",)),
    # Data
    (("data engineering",), ("Data Engineering",)),
    (("data science",), ("Data Science",)),
    (("data analysis", "analytics"), ("Data Analysis",)),
    (("business intelligence", "tableau"), ("Business Intelligence",)),
    (("power bi", "powerbi"), ("Power BI",)),
    (("data modeling", "data modelling", "database design"), ("Data Modeling",)),
    (
        ("data pipeline", "data ingestion", "airflow", "kafka"),
        ("Data Pipelines",),
    ),
    (("etl", "elt", "extract transform load"), ("JOBHUB_ETL",)),
    (("data warehouse", "data warehousing"), ("Data Warehousing",)),
    (("postgresql", "postgres"), ("PostgreSQL",)),
    (("sql server", "mssql"), ("SQL Server",)),
    (("mongodb", "mongo db"), ("MongoDB",)),
    (("snowflake",), ("Snowflake",)),
    (("databricks", "apache spark"), ("Databricks",)),
    # AI and ML: specific rules come before broad rules
    (("pytorch",), ("PyTorch",)),
    (("tensorflow",), ("TensorFlow",)),
    (("computer vision", "vision ai"), ("Computer Vision",)),
    (("mlops", "ml ops", "machine learning operations"), ("MLOps",)),
    (("generative ai", "genai", "gen ai"), ("Generative AI",)),
    (
        (
            "large language model",
            "llm",
            "prompt engineering",
            "ai agent",
            "agentic ai",
            "openai",
        ),
        ("Large Language Models",),
    ),
    (
        (
            "machine learning",
            "deep learning",
            "natural language processing",
            "nlp",
        ),
        ("Machine Learning",),
    ),
    (
        ("artificial intelligence", "ai-powered", "ai powered", "ai engineering"),
        ("Artificial Intelligence",),
    ),
    # Cloud and infrastructure
    (("amazon web services", "aws"), ("Amazon Web Services",)),
    (("microsoft azure", "azure"), ("Microsoft Azure",)),
    (("google cloud platform", "google cloud", "gcp"), ("Google Cloud Platform",)),
    (("docker", "containerization"), ("Docker",)),
    (("kubernetes", "k8s"), ("Kubernetes",)),
    (("terraform",), ("Terraform",)),
    (
        ("infrastructure as code", "infrastructure-as-code", "iac"),
        ("Infrastructure as Code",),
    ),
    (("linux",), ("Linux",)),
    (("distributed system",), ("Distributed Systems",)),
    (
        ("site reliability", "reliability engineering", "sre", "high availability"),
        ("Reliability Engineering",),
    ),
    (("observability", "monitoring"), ("Observability",)),
    (("devops",), ("DevOps",)),
    (
        (
            "ci/cd",
            "ci cd",
            "continuous integration",
            "continuous delivery",
            "github actions",
            "jenkins",
        ),
        ("CI/CD",),
    ),
    # Testing and security
    (("unit test",), ("Unit Testing",)),
    (
        ("integration test", "api testing", "database testing", "postman"),
        ("Integration Testing",),
    ),
    (
        ("end-to-end", "end to end test", "e2e", "playwright", "cypress", "selenium"),
        ("End-to-End Testing",),
    ),
    (("test automation", "automated testing", "qa automation"), ("Test Automation",)),
    (("software testing", "testing framework", "test case"), ("Software Testing",)),
    (("quality assurance",), ("Quality Assurance",)),
    (("code review",), ("Code Review",)),
    (("debugging",), ("Debugging",)),
    (
        ("identity and access management", "iam"),
        ("Identity and Access Management",),
    ),
    (
        (
            "security",
            "application security",
            "cloud security",
            "data security",
            "security clearance",
        ),
        ("Security",),
    ),
    # Engineering and delivery
    (("full stack", "full-stack", "fullstack"), ("Full Stack Development",)),
    (("system design",), ("System Design",)),
    (
        ("system architecture", "software architecture", "architecture"),
        ("Software Architecture",),
    ),
    (("platform engineering", "developer experience"), ("Platform Engineering",)),
    (("product engineering", "prototyping", "robotics"), ("Software Engineering",)),
    (
        ("software engineer", "software engineering", "software development"),
        ("Software Engineering",),
    ),
    (
        ("workflow automation", "process automation", "automation framework"),
        ("Automation",),
    ),
    (
        ("deployment", "deploying", "release management", "production rollout"),
        ("Deployment",),
    ),
    (
        ("performance optimization", "cost optimization", "scalability"),
        ("Performance Optimization",),
    ),
    # Employment
    (
        (
            "years of experience",
            "year of experience",
            "minimum years experience",
            "equivalent work experience",
        ),
        ("Years of Experience",),
    ),
    (("fully remote", "remote work", "remote"), ("Remote",)),
    (("hybrid",), ("Hybrid",)),
    (("on-site", "onsite", "on site"), ("On-site",)),
    (
        ("employment sponsorship", "visa sponsorship", "immigration sponsorship"),
        ("Employment Sponsorship",),
    ),
    (
        ("contract-to-hire", "contract to hire", "job type contract", "c2c"),
        ("Contract",),
    ),
    (("travel required", "percentage of travel", "travel"), ("Travel",)),
    (
        ("relocation assistance", "relocation support", "relocation"),
        ("Relocation Assistance",),
    ),
    # Geography
    (("united states", "united states of america", "usa"), ("United States",)),
    (("canada",), ("Canada",)),
    (("united kingdom", "great britain", "england"), ("United Kingdom",)),
    (("europe",), ("Europe",)),
)

_COMPILED_KEYWORD_RULES: tuple[
    tuple[tuple[re.Pattern[str], ...], tuple[str, ...]],
    ...,
] = tuple(
    (
        tuple(_phrase_pattern(needle) for needle in needles),
        canonical_values,
    )
    for needles, canonical_values in KEYWORD_RULES
)


# ---------------------------------------------------------------------------
# Location recognition
# ---------------------------------------------------------------------------

US_STATE_NAMES: frozenset[str] = frozenset(
    {
        "alabama",
        "alaska",
        "arizona",
        "arkansas",
        "california",
        "colorado",
        "connecticut",
        "delaware",
        "florida",
        "georgia",
        "hawaii",
        "idaho",
        "illinois",
        "indiana",
        "iowa",
        "kansas",
        "kentucky",
        "louisiana",
        "maine",
        "maryland",
        "massachusetts",
        "michigan",
        "minnesota",
        "mississippi",
        "missouri",
        "montana",
        "nebraska",
        "nevada",
        "new hampshire",
        "new jersey",
        "new mexico",
        "new york",
        "north carolina",
        "north dakota",
        "ohio",
        "oklahoma",
        "oregon",
        "pennsylvania",
        "rhode island",
        "south carolina",
        "south dakota",
        "tennessee",
        "texas",
        "utah",
        "vermont",
        "virginia",
        "washington",
        "west virginia",
        "wisconsin",
        "wyoming",
        "district of columbia",
    }
)

US_LOCATION_NAMES: frozenset[str] = frozenset(
    {
        "atlanta",
        "austin",
        "bellevue",
        "boston",
        "charlotte",
        "dallas",
        "denver",
        "los angeles",
        "new york city",
        "oakland",
        "palo alto",
        "san francisco",
        "seattle",
        "washington dc",
        "washington d.c",
    }
)

CANADA_LOCATIONS: frozenset[str] = frozenset(
    {"toronto", "vancouver", "montreal", "ottawa", "calgary", "victoria", "waterloo"}
)

UK_LOCATIONS: frozenset[str] = frozenset(
    {"london", "manchester", "birmingham", "edinburgh", "glasgow", "bristol", "leeds"}
)

EUROPE_LOCATIONS: frozenset[str] = frozenset(
    {
        "berlin",
        "munich",
        "hamburg",
        "frankfurt",
        "paris",
        "amsterdam",
        "madrid",
        "barcelona",
        "lisbon",
        "prague",
        "warsaw",
        "stockholm",
        "helsinki",
        "copenhagen",
        "vienna",
        "zurich",
        "dublin",
    }
)


def _contains_location(key: str, names: frozenset[str]) -> bool:
    return any(_phrase_pattern(name).search(key) for name in names)


def _location_topics(key: str) -> tuple[str, ...]:
    if _contains_location(key, CANADA_LOCATIONS):
        return ("Canada",)
    if _contains_location(key, UK_LOCATIONS):
        return ("United Kingdom",)
    if _contains_location(key, EUROPE_LOCATIONS):
        return ("Europe",)
    if _contains_location(key, US_LOCATION_NAMES):
        return ("United States",)
    if any(_phrase_pattern(state).search(key) for state in US_STATE_NAMES):
        return ("United States",)
    if _phrase_pattern("united states").search(key):
        return ("United States",)
    if _phrase_pattern("usa").search(key):
        return ("United States",)
    if _phrase_pattern("canada").search(key):
        return ("Canada",)
    if _phrase_pattern("united kingdom").search(key):
        return ("United Kingdom",)
    if _phrase_pattern("europe").search(key):
        return ("Europe",)
    return ()


# ---------------------------------------------------------------------------
# Public normalization functions
# ---------------------------------------------------------------------------


def validate_topic(topic: str) -> str:
    if topic not in CANONICAL_TOPIC_SET:
        raise ValueError(f"Unknown canonical topic: {topic!r}")
    return topic


def normalize_topic(
    raw_topic: str,
    *,
    fallback: str = "Other",
) -> tuple[str, ...]:
    if not raw_topic or not raw_topic.strip():
        return ()

    raw_topic = raw_topic.strip()

    if raw_topic in CANONICAL_TOPIC_SET:
        return (raw_topic,)

    key = _normalized_key(raw_topic)

    exact = _NORMALIZED_ALIASES.get(key)
    if exact:
        return exact

    matches: list[str] = []

    for patterns, canonical_values in _COMPILED_KEYWORD_RULES:
        if not any(pattern.search(key) for pattern in patterns):
            continue

        for canonical in canonical_values:
            if canonical in CANONICAL_TOPIC_SET and canonical not in matches:
                matches.append(canonical)

    for canonical in _location_topics(key):
        if canonical not in matches:
            matches.append(canonical)

    if matches:
        return tuple(matches)

    return (validate_topic(fallback),)


def normalize_topics(
    raw_topics: str | Iterable[str],
    *,
    fallback: str = "Other",
    max_topics: int = 4,
) -> list[str]:
    if max_topics <= 0:
        raise ValueError("max_topics must be greater than zero.")

    validate_topic(fallback)

    if isinstance(raw_topics, str):
        raw_value = raw_topics.strip()
        normalized_key = _normalized_key(raw_value)

        if normalized_key in _NORMALIZED_ALIASES:
            values = [raw_value]
        else:
            values = re.split(r"[,;]", raw_value)
    else:
        values = list(raw_topics)

    canonical: list[str] = []

    for raw in values:
        for topic in normalize_topic(str(raw), fallback=fallback):
            if topic not in canonical:
                canonical.append(topic)

    meaningful = [topic for topic in canonical if topic != fallback]
    if meaningful:
        canonical = meaningful

    if not canonical:
        canonical = [fallback]

    return canonical[:max_topics]


def category_for(topic: str) -> str:
    validate_topic(topic)

    for category, topics in TOPICS_BY_CATEGORY.items():
        if topic in topics:
            return category

    raise RuntimeError(f"Canonical topic has no category: {topic!r}")


def all_topics() -> Sequence[str]:
    return CANONICAL_TOPICS


def canonical_topic_options() -> list[dict[str, str]]:
    """Return the only Notion option objects permitted for active topic fields."""
    return [{"name": topic} for topic in CANONICAL_TOPICS]


def notion_multi_select(
    topics: Iterable[str],
) -> dict[str, list[dict[str, str]]]:
    unique: list[str] = []

    for topic in topics:
        validate_topic(topic)
        if topic not in unique:
            unique.append(topic)

    return {"multi_select": [{"name": topic} for topic in unique]}


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"{len(CANONICAL_TOPICS)} canonical topics")

    for category, topics in TOPICS_BY_CATEGORY.items():
        print(f"\n{category} ({len(topics)})")
        for topic in topics:
            print(f"  - {topic}")
