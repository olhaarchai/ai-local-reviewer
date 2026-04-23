from typing import Literal, TypeAlias

Severity: TypeAlias = Literal["Critical", "High", "Medium", "Low"]

TimingEntry: TypeAlias = dict[str, float]
Timings: TypeAlias = list[TimingEntry]
