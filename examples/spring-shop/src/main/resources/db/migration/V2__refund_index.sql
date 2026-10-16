CREATE INDEX idx_refunds_order ON refunds (order_id);
CREATE INDEX idx_refunds_order_dup ON refunds (order_id);
DROP INDEX idx_refunds_order_dup;
