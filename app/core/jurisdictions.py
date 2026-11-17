"""
Jurisdiction packs: what changes when a dispute is governed by a different
country's law — currency, courts and tribunals, the statutes cited, the
pre-action notice period, governing-law and e-signature clauses, police
contacts, and the instructions given to the LLM.

India ("IN") is the default and keeps the detailed rule set in
app/agents/offline_analysis.py and app/core/legal_reference.py. The other
packs give a sound general framework; claim limits and statutes change, so
have them reviewed by a qualified lawyer in that jurisdiction before launch.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Jurisdiction:
    code: str
    name: str
    currency: str
    symbol: str
    locale: str                     # for number formatting in the browser
    phone_cc: str
    governing_law: str              # "the laws of England and Wales"
    courts: str                     # "the courts of England and Wales"
    contract_law: str
    esign_law: str
    mediation_law: str
    data_law: str
    limitation: str
    limitation_years: int
    notice_days: int
    notice_note: str
    police: str
    forums: dict                    # track -> [(max_amount or None, forum name)]
    laws: dict                      # category -> [(act, section, relevance, strength)]
    payment_providers: tuple = ()
    llm_context: str = ""
    default_language: str = "en"
    languages: tuple = ("en",)
    extra: dict = field(default_factory=dict)

    def forum_for(self, track: str, amount) -> str:
        tiers = self.forums.get(track) or self.forums.get("monetary_civil") or []
        try:
            value = float(amount or 0)
        except (TypeError, ValueError):
            value = 0.0
        for limit, name in tiers:
            if limit is None or value <= limit:
                return name
        return tiers[-1][1] if tiers else "the competent court"

    def laws_for(self, category: str) -> list:
        return self.laws.get(category) or self.laws.get("contract_breach") or []

    def public(self) -> dict:
        return {"code": self.code, "name": self.name, "currency": self.currency, "symbol": self.symbol,
                "locale": self.locale, "phone_cc": self.phone_cc, "default_language": self.default_language,
                "languages": list(self.languages), "notice_days": self.notice_days}


_CONTRACT = "contract_breach"

INDIA = Jurisdiction(
    code="IN", name="India", currency="INR", symbol="Rs.", locale="en-IN", phone_cc="91",
    governing_law="the laws of India", courts="the courts at {city}",
    contract_law="Indian Contract Act, 1872",
    esign_law="Section 10A of the Information Technology Act, 2000",
    mediation_law="the Mediation Act, 2023 and the Arbitration and Conciliation Act, 1996",
    data_law="the Digital Personal Data Protection Act, 2023",
    limitation="Limitation Act, 1963 (generally 3 years for money claims)", limitation_years=3,
    notice_days=15, notice_note="A written legal notice giving 15 days to comply is customary.",
    police="Report to the police (dial 112 in an emergency) and request an FIR",
    forums={}, laws={}, payment_providers=("razorpay",),
    languages=("en", "hi", "bn", "mr", "ta", "te"),
    extra={"inr_rate": 1, "court": {"cost_min": 50000, "filing_fee": 5000, "lawyer_fees": 40000, "miscellaneous": 10000, "months": 30}},
)

ENGLAND_WALES = Jurisdiction(
    code="GB", name="England and Wales", currency="GBP", symbol="£", locale="en-GB", phone_cc="44",
    governing_law="the laws of England and Wales", courts="the courts of England and Wales",
    contract_law="the common law of contract (England and Wales)",
    esign_law="section 7 of the Electronic Communications Act 2000",
    mediation_law="the Civil Procedure Rules and the court's power to order ADR (Churchill v Merthyr Tydfil CBC [2023] EWCA Civ 1416)",
    data_law="the UK GDPR and the Data Protection Act 2018",
    limitation="Limitation Act 1980, s.5 (6 years for contract claims)", limitation_years=6,
    notice_days=30,
    notice_note=("Follow the Practice Direction on Pre-Action Conduct; for a business claiming a debt from an "
                 "individual, the Pre-Action Protocol for Debt Claims requires a Letter of Claim and 30 days to reply."),
    police="Report to the police (999 in an emergency, 101 otherwise)",
    forums={
        "monetary_civil": [(10_000, "County Court — small claims track (Online Civil Money Claims)"),
                           (25_000, "County Court — fast track"), (None, "County Court / High Court — multi-track")],
        "consumer":       [(10_000, "County Court — small claims track"), (None, "County Court")],
        "employment":     [(None, "Employment Tribunal (after ACAS Early Conciliation)")],
        "non_monetary":   [(None, "County Court")],
        "criminal":       [(None, "Police / Crown Prosecution Service")],
    },
    laws={
        _CONTRACT: [("Common law of contract", "Damages for breach",
                     "The innocent party may recover losses flowing naturally from the breach (Hadley v Baxendale).", "PRIMARY"),
                    ("Late Payment of Commercial Debts (Interest) Act 1998", "Statutory interest",
                     "Business-to-business invoices carry statutory interest and fixed recovery costs.", "SECONDARY")],
        "loan_default": [("Common law of contract", "Debt claim",
                          "A sum due under a loan agreement is recoverable as a debt.", "PRIMARY"),
                         ("Consumer Credit Act 1974", "Default notices (regulated agreements)",
                          "Regulated consumer credit requires a default notice before enforcement.", "SECONDARY")],
        "cheque_bounce": [("Bills of Exchange Act 1882", "Dishonour of a cheque",
                           "The holder of a dishonoured cheque may sue on the cheque itself.", "PRIMARY")],
        "rental_deposit": [("Housing Act 2004", "Sections 212–215",
                            "Deposits under assured shorthold tenancies must be protected; non-compliance carries penalties.", "PRIMARY"),
                           ("Common law of contract", "Tenancy agreement",
                            "Deductions from a deposit must be justified under the tenancy agreement.", "SECONDARY")],
        "unpaid_rent": [("Common law of contract", "Rent arrears",
                         "Unpaid rent is recoverable as a debt under the tenancy agreement.", "PRIMARY")],
        "unpaid_salary": [("Employment Rights Act 1996", "Part II (s.13, s.23)",
                           "Unauthorised deductions from wages can be claimed in the Employment Tribunal.", "PRIMARY")],
        "wrongful_termination": [("Employment Rights Act 1996", "Section 94 (unfair dismissal)",
                                  "Employees with qualifying service have the right not to be unfairly dismissed.", "PRIMARY")],
        "consumer_general": [("Consumer Rights Act 2015", "Sections 9–24 and 49",
                              "Goods must be of satisfactory quality; services performed with reasonable care and skill.", "PRIMARY")],
        "defamation": [("Defamation Act 2013", "Section 1 (serious harm)",
                        "A statement is defamatory only if it has caused or is likely to cause serious harm.", "PRIMARY")],
    },
    llm_context=("This dispute is governed by the law of England and Wales. Cite only the law of England and Wales "
                 "(statutes, the Civil Procedure Rules and English case law) and use GBP (£). Courts: County Court "
                 "small claims track up to £10,000, fast track up to £25,000; Employment Tribunal for employment "
                 "claims after ACAS Early Conciliation. Follow the Practice Direction on Pre-Action Conduct."),
    extra={"inr_rate": 110, "court": {"cost_min": 500, "filing_fee": 205, "lawyer_fees": 0, "miscellaneous": 150, "months": 12}},
)

SINGAPORE = Jurisdiction(
    code="SG", name="Singapore", currency="SGD", symbol="S$", locale="en-SG", phone_cc="65",
    governing_law="the laws of Singapore", courts="the courts of Singapore",
    contract_law="the common law of contract (Singapore)",
    esign_law="the Electronic Transactions Act 2010",
    mediation_law="the Mediation Act 2017 (a mediated settlement may be recorded as an order of court)",
    data_law="the Personal Data Protection Act 2012",
    limitation="Limitation Act 1959, s.6 (6 years for contract claims)", limitation_years=6,
    notice_days=14, notice_note="Send a letter of demand giving reasonable time (commonly 14 days) to pay.",
    police="Report to the police (999 in an emergency, or file a report online)",
    forums={
        "monetary_civil": [(20_000, "Small Claims Tribunals (up to S$30,000 with written consent)"),
                           (60_000, "Magistrate's Court"), (250_000, "District Court"), (None, "General Division of the High Court")],
        "consumer":       [(20_000, "Small Claims Tribunals"), (None, "Magistrate's Court / District Court")],
        "employment":     [(20_000, "Employment Claims Tribunals (after TADM mediation)"), (None, "State Courts")],
        "non_monetary":   [(None, "Community Disputes Resolution Tribunals / State Courts")],
        "criminal":       [(None, "Singapore Police Force")],
    },
    laws={
        _CONTRACT: [("Common law of contract", "Damages for breach",
                     "The innocent party may recover losses caused by the breach.", "PRIMARY"),
                    ("Small Claims Tribunals Act 1984", "Jurisdiction",
                     "Contract and property-damage claims up to S$20,000 (S$30,000 by consent) are heard cheaply and quickly.", "SECONDARY")],
        "loan_default": [("Common law of contract", "Debt claim",
                          "A sum due under a loan agreement is recoverable as a debt.", "PRIMARY")],
        "cheque_bounce": [("Bills of Exchange Act 1949", "Dishonour of a cheque",
                           "The holder of a dishonoured cheque may sue on the cheque.", "PRIMARY")],
        "rental_deposit": [("Common law of contract", "Tenancy agreement",
                            "Deposit deductions must be justified under the tenancy agreement.", "PRIMARY"),
                           ("Small Claims Tribunals Act 1984", "Residential tenancy claims",
                            "Disputes on residential leases of up to two years fall within the tribunals.", "SECONDARY")],
        "unpaid_salary": [("Employment Claims Act 2016", "Salary-related claims",
                           "Salary claims are mediated by TADM and then heard by the Employment Claims Tribunals.", "PRIMARY"),
                          ("Employment Act 1968", "Payment of salary",
                           "Salary must be paid within 7 days after the end of the salary period.", "SECONDARY")],
        "wrongful_termination": [("Employment Act 1968", "Section 14 (wrongful dismissal)",
                                  "Employees may claim for dismissal without just cause or excuse.", "PRIMARY")],
        "consumer_general": [("Consumer Protection (Fair Trading) Act 2003", "Unfair practices and lemon law",
                              "Consumers may seek repair, replacement or refund for defective goods.", "PRIMARY")],
        "defamation": [("Defamation Act 1957", "Defamation",
                        "Publication of false statements damaging reputation is actionable.", "PRIMARY")],
    },
    llm_context=("This dispute is governed by Singapore law. Cite only Singapore statutes and case law and use SGD (S$). "
                 "Forums: Small Claims Tribunals up to S$20,000 (S$30,000 by consent), Magistrate's Court up to "
                 "S$60,000, District Court up to S$250,000; employment salary claims via TADM mediation then the "
                 "Employment Claims Tribunals. The Mediation Act 2017 lets mediated settlements be recorded as court orders."),
    extra={"inr_rate": 65, "court": {"cost_min": 300, "filing_fee": 100, "lawyer_fees": 0, "miscellaneous": 100, "months": 4}},
)

UAE = Jurisdiction(
    code="AE", name="United Arab Emirates", currency="AED", symbol="AED", locale="en-AE", phone_cc="971",
    governing_law="the federal laws of the United Arab Emirates as applied in {city}",
    courts="the competent courts of {city}",
    contract_law="the Civil Transactions Law (Federal Law No. 5 of 1985)",
    esign_law="Federal Decree-Law No. 46 of 2021 on Electronic Transactions and Trust Services",
    mediation_law="Federal Law No. 6 of 2021 on Mediation for the Settlement of Civil and Commercial Disputes",
    data_law="Federal Decree-Law No. 45 of 2021 on the Protection of Personal Data",
    limitation="Civil Transactions Law (15 years general; commercial obligations generally 10 years)", limitation_years=10,
    notice_days=15, notice_note="Send a formal notice (often via notary) before filing; many claims first go to an amicable settlement centre.",
    police="Report to the police (999 in an emergency)",
    forums={
        "monetary_civil": [(None, "Centre for Amicable Settlement of Disputes / Court of First Instance")],
        "consumer":       [(None, "Consumer protection authority of the Emirate / Court of First Instance")],
        "employment":     [(None, "Ministry of Human Resources and Emiratisation (MOHRE), then the Labour Court")],
        "non_monetary":   [(None, "Court of First Instance")],
        "criminal":       [(None, "Police / Public Prosecution")],
    },
    laws={
        _CONTRACT: [("Civil Transactions Law (Federal Law No. 5 of 1985)", "Contractual liability",
                     "A party who fails to perform a contract is liable to compensate the other.", "PRIMARY"),
                    ("Commercial Transactions Law (Federal Decree-Law No. 50 of 2022)", "Commercial obligations",
                     "Governs commercial debts between merchants, including interest on late payment.", "SECONDARY")],
        "cheque_bounce": [("Commercial Transactions Law (Federal Decree-Law No. 50 of 2022)", "Cheques",
                           "A dishonoured cheque is an enforceable instrument; the holder may seek execution directly.", "PRIMARY")],
        "rental_deposit": [("Emirate tenancy law (e.g. Dubai Law No. 26 of 2007)", "Deposits and rental disputes",
                            "Rental disputes go to the Emirate's rental disputes centre.", "PRIMARY")],
        "unpaid_rent": [("Emirate tenancy law (e.g. Dubai Law No. 26 of 2007)", "Rent arrears",
                         "Rental disputes go to the Emirate's rental disputes centre.", "PRIMARY")],
        "unpaid_salary": [("Federal Decree-Law No. 33 of 2021 (Labour Law)", "Payment of wages",
                           "Wages must be paid on time; complaints start with MOHRE.", "PRIMARY")],
        "wrongful_termination": [("Federal Decree-Law No. 33 of 2021 (Labour Law)", "Termination",
                                  "Compensation is available for unlawful termination.", "PRIMARY")],
        "consumer_general": [("Federal Law No. 15 of 2020 on Consumer Protection", "Consumer rights",
                              "Suppliers must provide goods and services as described and honour warranties.", "PRIMARY")],
    },
    llm_context=("This dispute is governed by UAE federal law (onshore; not DIFC or ADGM). Cite only UAE federal "
                 "and Emirate law and use AED. Many civil claims first go to a Centre for Amicable Settlement of "
                 "Disputes; employment complaints start with MOHRE; rental disputes go to the Emirate's rental "
                 "disputes centre. Mediation is governed by Federal Law No. 6 of 2021."),
    languages=("en", "ar"),
    extra={"inr_rate": 23, "court": {"cost_min": 5000, "filing_fee": 1500, "lawyer_fees": 7500, "miscellaneous": 1000, "months": 8}},
)

UNITED_STATES = Jurisdiction(
    code="US", name="United States", currency="USD", symbol="$", locale="en-US", phone_cc="1",
    governing_law="the laws of the State of {state}", courts="the state courts of {state}",
    contract_law="the contract law of the relevant state (and UCC Article 2 for sales of goods)",
    esign_law="the federal ESIGN Act (15 U.S.C. § 7001) and the state's Uniform Electronic Transactions Act",
    mediation_law="the state's mediation and settlement rules",
    data_law="applicable state privacy laws",
    limitation="state statutes of limitation (commonly 3–6 years for contract claims)", limitation_years=4,
    notice_days=30, notice_note="Send a written demand letter; small claims courts expect an attempt to settle first.",
    police="Report to the police (911 in an emergency)",
    forums={
        "monetary_civil": [(5_000, "Small claims court (limits vary by state, typically $5,000–$25,000)"),
                           (None, "State civil court")],
        "consumer":       [(5_000, "Small claims court"), (None, "State civil court")],
        "employment":     [(None, "State labor department wage claim / court")],
        "non_monetary":   [(None, "State civil court")],
        "criminal":       [(None, "Local police / district attorney")],
    },
    laws={
        _CONTRACT: [("State contract law", "Breach of contract",
                     "The non-breaching party may recover expectation damages.", "PRIMARY")],
        "loan_default": [("State contract law", "Debt claim", "Amounts due under a loan agreement are recoverable.", "PRIMARY"),
                         ("Fair Debt Collection Practices Act (15 U.S.C. § 1692)", "Third-party debt collectors",
                          "Debt collectors acting for others must follow federal collection rules.", "SUPPORTING")],
        "unpaid_salary": [("Fair Labor Standards Act", "Minimum wage and overtime",
                           "Federal minimum wage and overtime apply; state wage-payment laws add deadlines and penalties.", "PRIMARY")],
        "consumer_general": [("State consumer protection (UDAP) statutes", "Unfair or deceptive practices",
                              "Consumers may recover for unfair or deceptive business practices.", "PRIMARY"),
                             ("Magnuson-Moss Warranty Act", "Written warranties", "Enforces written consumer warranties.", "SECONDARY")],
    },
    llm_context=("This dispute is governed by United States law; the claimant's state is given as 'State'. Cite the "
                 "law of that state and federal law where relevant, and use USD ($). Small claims limits vary by state. "
                 "If the state is unknown, say that state law must be confirmed rather than guessing."),
    languages=("en", "es"),
    extra={"inr_rate": 85, "court": {"cost_min": 400, "filing_fee": 100, "lawyer_fees": 0, "miscellaneous": 150, "months": 6}},
)

JURISDICTIONS = {j.code: j for j in (INDIA, ENGLAND_WALES, SINGAPORE, UAE, UNITED_STATES)}
DEFAULT_JURISDICTION = "IN"
_ALIASES = {"UK": "GB", "ENGLAND": "GB", "IND": "IN", "UAE": "AE", "USA": "US", "SGP": "SG"}


def normalize_jurisdiction(value) -> str | None:
    if not value:
        return None
    v = str(value).strip().upper()
    v = _ALIASES.get(v, v)
    return v if v in JURISDICTIONS else None


def case_jurisdiction(case: dict | None) -> Jurisdiction:
    return JURISDICTIONS.get(normalize_jurisdiction((case or {}).get("jurisdiction")) or DEFAULT_JURISDICTION)


def money(amount, currency: str | dict | None = None) -> str:
    """'Rs. 36,000', '£1,250', 'S$800', 'AED 5,000', '$300'."""
    if isinstance(currency, dict):
        currency = currency.get("currency") or case_jurisdiction(currency).currency
    code = (currency or "INR").upper()
    try:
        value = float(amount or 0)
    except (TypeError, ValueError):
        value = 0.0
    text = f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
    symbol = next((j.symbol for j in JURISDICTIONS.values() if j.currency == code), code)
    return f"{symbol} {text}" if symbol[-1].isalpha() or symbol.endswith(".") else f"{symbol}{text}"


def localize_prompt(system_prompt: str, case: dict | None) -> str:
    """Prepend the jurisdiction override for non-Indian cases."""
    j = case_jurisdiction(case)
    if j.code == DEFAULT_JURISDICTION or not j.llm_context:
        return system_prompt
    return (f"JURISDICTION OVERRIDE — this takes precedence over anything below.\n{j.llm_context}\n"
            "Where the instructions below mention Indian statutes, courts, procedures or rupees, apply the "
            "equivalent concept of this jurisdiction instead and never cite Indian law.\n\n" + system_prompt)
