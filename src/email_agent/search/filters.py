from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass
class SearchFilters:
    date_from: date | None = None
    date_to: date | None = None  # inclusive
    sender: str | None = None  # substring of address or display name
    category: str | None = None
    labels: list[str] = field(default_factory=list)

    def chunk_sql(self, account_id: int, alias: str = "c") -> tuple[str, list]:
        """SQL predicates over the chunks table (filter columns are denormalized onto it).

        Always scoped to one account: retrieval never crosses mailboxes.
        """
        clauses, params = [f"{alias}.account_id = %s"], [account_id]
        if self.date_from:
            clauses.append(f"{alias}.sent_at >= %s")
            params.append(self.date_from)
        if self.date_to:
            clauses.append(f"{alias}.sent_at < %s")
            params.append(self.date_to + timedelta(days=1))
        if self.category:
            clauses.append(f"{alias}.category = %s")
            params.append(self.category)
        if self.sender:
            clauses.append(
                f"({alias}.from_addr ILIKE %s OR {alias}.email_id IN "
                "(SELECT id FROM emails WHERE from_name ILIKE %s))"
            )
            params += [f"%{self.sender}%"] * 2
        if self.labels:
            clauses.append(f"{alias}.email_id IN (SELECT id FROM emails WHERE labels && %s)")
            params.append(self.labels)
        return " AND ".join(clauses), params
