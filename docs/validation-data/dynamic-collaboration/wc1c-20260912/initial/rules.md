# Replenishment rules
stock and target are non-negative integers; pack is a positive integer.
A negative stock or target, or a non-positive pack, raises ValueError.
Validate those limits before applying the blocked flag.
Blocked products get zero replenishment.
When stock already reaches target, return zero.
Otherwise order the smallest whole number of packs that reaches or exceeds target.
Return the ordered unit count, not the number of packs.
