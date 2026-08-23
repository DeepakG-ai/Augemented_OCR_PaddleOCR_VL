//! syteline_connector.rs ← syteline_connector.py.
//!
//! SyteLine PO schema field definitions used to seed the PO Automation output
//! schema. The Python module held data only (despite the name, it never
//! connected anywhere), so the port is a faithful pair of constant lists.

pub const PO_HEADER_FIELDS: [&str; 7] = [
    "CustNum",
    "Client",
    "CustPo",
    "Zip",
    "Addr1",
    "ShipToaddr",
    "OrderDate",
];

pub const PO_LINE_FIELDS: [&str; 8] = [
    "Line",
    "Item",
    "ItemVariant",
    "CustItem",
    "QtyOrdered",
    "Price",
    "UM",
    "DueDate",
];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn field_lists_match_the_syteline_schema_exactly() {
        assert_eq!(
            PO_HEADER_FIELDS,
            ["CustNum", "Client", "CustPo", "Zip", "Addr1", "ShipToaddr", "OrderDate"]
        );
        assert_eq!(
            PO_LINE_FIELDS,
            ["Line", "Item", "ItemVariant", "CustItem", "QtyOrdered", "Price", "UM", "DueDate"]
        );
        assert!(PO_HEADER_FIELDS
            .iter()
            .all(|f| !PO_LINE_FIELDS.contains(f)));
    }
}
