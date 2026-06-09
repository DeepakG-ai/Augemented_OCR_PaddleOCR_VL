# SyteLine PO schema field definitions — used to seed the PO Automation output schema.

PO_HEADER_FIELDS = [
    "CustNum", "Client", "CustPo", "Zip", "Addr1", "ShipToaddr", "OrderDate"
]

PO_LINE_FIELDS = [
    "Line", "Item", "ItemVariant", "CustItem", "QtyOrdered", "Price", "UM", "DueDate"
]
