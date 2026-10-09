-- Streaming responses briefly stored SDK-only fields in conversation history, which the API
-- rejects when the conversation continues. Remove them from any stored item.
UPDATE conversation_items SET item = item - 'parsed_arguments' WHERE item ? 'parsed_arguments';
UPDATE conversation_items ci SET item = jsonb_set(
    ci.item, '{content}',
    (SELECT jsonb_agg(block - 'parsed') FROM jsonb_array_elements(ci.item->'content') AS block))
WHERE jsonb_typeof(ci.item->'content') = 'array'
  AND EXISTS (SELECT 1 FROM jsonb_array_elements(ci.item->'content') AS b WHERE b ? 'parsed');
