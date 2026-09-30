import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RobotsPolicy:
    """Wildcard and end-anchor rules used by Kupi's public robots.txt."""

    rules: tuple[tuple[str, bool], ...]
    delay: float = 0

    @classmethod
    def parse(cls, content: str, user_agent: str) -> "RobotsPolicy":
        groups: list[tuple[list[str], list[tuple[str, bool]], float]] = []
        agents: list[str] = []
        rules: list[tuple[str, bool]] = []
        delay = 0.0
        has_directives = False
        for raw in content.splitlines() + ["User-agent: __end__"]:
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if has_directives:
                    groups.append((agents, rules, delay))
                    agents, rules, delay, has_directives = [], [], 0.0, False
                agents.append(value.lower())
            elif key in {"allow", "disallow"} and agents:
                has_directives = True
                if value:
                    rules.append((value, key == "allow"))
            elif key == "crawl-delay" and agents:
                has_directives = True
                delay = max(0, float(value))
        token = user_agent.lower().split("/", 1)[0]
        specificity = max(
            (len(a) for names, _, _ in groups for a in names if a != "*" and a in token),
            default=0,
        )
        chosen = [
            (r, d)
            for names, r, d in groups
            if any(a != "*" and a in token and len(a) == specificity for a in names)
            or (not specificity and "*" in names)
        ]
        return cls(
            tuple(rule for rules, _ in chosen for rule in rules),
            max((delay for _, delay in chosen), default=0),
        )

    def permits(self, url: str) -> bool:
        parsed = urlsplit(url)
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        matches: list[tuple[int, bool]] = []
        for pattern, allowed in self.rules:
            terminal = pattern.endswith("$")
            body = pattern[:-1] if terminal else pattern
            expression = "^" + re.escape(body).replace(r"\*", ".*") + ("$" if terminal else "")
            if re.search(expression, target):
                matches.append((len(body.replace("*", "")), allowed))
        return max(matches, default=(0, True))[1]
