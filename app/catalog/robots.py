"""Правила robots.txt с поддержкой масок * и завершающего $."""

import math
import re
from dataclasses import dataclass, field


@dataclass
class RobotGroup:
    """Правила для одной группы роботов."""

    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)
    delay: float = 0


class RobotsPolicy:
    """Применить наиболее конкретную группу и наиболее длинное правило."""

    def __init__(self, content: str, agent: str):
        groups: list[RobotGroup] = []
        group = RobotGroup()
        has_directives = False
        for line in content.splitlines():
            key, separator, value = line.split("#", 1)[0].partition(":")
            if not separator:
                continue
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if has_directives:
                    groups.append(group)
                    group = RobotGroup()
                    has_directives = False
                group.agents.append(value.lower())
            elif group.agents:
                has_directives = True
                if key in {"allow", "disallow"} and value:
                    group.rules.append((key == "allow", value))
                elif key == "crawl-delay":
                    try:
                        delay = float(value)
                        if math.isfinite(delay) and delay >= 0:
                            group.delay = delay
                    except ValueError:
                        pass
        if group.agents:
            groups.append(group)
        scores = [
            max(
                (len(a) if a != "*" else 0 for a in g.agents if a == "*" or a in agent.lower()),
                default=-1,
            )
            for g in groups
        ]
        best = max(scores, default=-1)
        selected = [
            g for g, score in zip(groups, scores, strict=True) if score == best and score >= 0
        ]
        self.rules = [rule for g in selected for rule in g.rules]
        self.delay = max((g.delay for g in selected), default=0)

    def allows(self, path: str) -> bool:
        """Проверить путь вместе с query; при равной длине Allow имеет приоритет."""
        matches: list[tuple[int, bool]] = []
        for allow, pattern in self.rules:
            anchored = pattern.endswith("$")
            value = pattern[:-1] if anchored else pattern
            regex = "^" + re.escape(value).replace(r"\*", ".*") + ("$" if anchored else "")
            if re.search(regex, path):
                matches.append((len(value.replace("*", "")), allow))
        return max(matches)[1] if matches else True
