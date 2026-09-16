# OutputFormatter

Format a classification result dict as a JSON string.

## Input
A dict with keys: ticket_id (str), priority (str), category (str), confidence (float), justification (str).

## Output
A valid JSON string. Raises ValueError if required keys are missing or confidence is out of [0.0, 1.0].

## Edge cases
- Missing required key → ValueError
- confidence < 0 or > 1 → ValueError
- Extra keys in input → ignored
