<!--
  Example domain pack for Microsoft's public AdventureWorks sample database (OLTP version,
  e.g. AdventureWorks2022). Use it as a model when writing rules for your own database.
-->

## 1. What this data is about

AdventureWorks is a fictional company that manufactures and sells bicycles, components,
clothing and accessories. It sells online to individual customers and through resellers
(bike shops). The data covers sales, products and inventory, manufacturing work orders,
purchasing from vendors, and employees.

The orders in this sample are historical (roughly 2011-2014). Relative periods such as
"last year" or "the last 3 months" are measured from the LATEST order date in the data
(`MAX(OrderDate)` in `Sales.SalesOrderHeader`), not from today's date — otherwise every
such question would return nothing.

## 2. Glossary — business word → where it lives

| Business term | Meaning | Table / column |
|---|---|---|
| Order | One customer purchase | `Sales.SalesOrderHeader` (one row per order) |
| Order line | One product within an order | `Sales.SalesOrderDetail` |
| Revenue / sales | Order value before tax and freight | `Sales.SalesOrderHeader.SubTotal`, or `SUM(Sales.SalesOrderDetail.LineTotal)` per line |
| Total due | What the customer pays, including tax and freight | `Sales.SalesOrderHeader.TotalDue` |
| Online order | Placed by an individual on the website | `Sales.SalesOrderHeader.OnlineOrderFlag = 1` |
| Reseller order | Placed by a store through a sales person | `Sales.SalesOrderHeader.OnlineOrderFlag = 0` |
| Territory / region | Sales region | `Sales.SalesTerritory.Name`; its wider area is `[Group]` (North America, Europe, Pacific) |
| Product category | Bikes, Components, Clothing, Accessories | `Production.ProductCategory.Name` |
| Product subcategory | e.g. Mountain Bikes, Helmets | `Production.ProductSubcategory.Name` |
| Stock on hand | Units in stock, summed over all locations | `SUM(Production.ProductInventory.Quantity)` per product |
| Employee | A person on the payroll | `HumanResources.Employee` (`BusinessEntityID` = `Person.Person.BusinessEntityID`) |
| Vendor | A supplier the company buys from | `Purchasing.Vendor` |

## 3. Table grain and duplicates

* `Sales.SalesOrderDetail` has one row per product per order. Count orders with
  `COUNT(DISTINCT SalesOrderID)`, never `COUNT(*)` on the detail table.
* Do NOT add up `SubTotal` or `TotalDue` after joining the header to its detail lines — the header
  value repeats on every line and the total is multiplied. For revenue by product, sum
  `LineTotal` from the detail table; for revenue by order, customer or territory, sum `SubTotal`
  from the header alone.
* `Production.ProductInventory` has one row per product per location. Sum `Quantity` per product.
* `HumanResources.EmployeeDepartmentHistory` and `HumanResources.EmployeePayHistory` keep
  history: several rows per employee.

## 4. Joins that are not obvious

* A customer is either a person or a store. `Sales.Customer.PersonID` → `Person.Person` (online
  customers, name = `FirstName + ' ' + LastName`); `Sales.Customer.StoreID` → `Sales.Store`
  (resellers, name = `Sales.Store.Name`). One of the two is usually NULL.
* Product → category goes through the subcategory:
  `Production.Product.ProductSubcategoryID` → `Production.ProductSubcategory.ProductCategoryID` →
  `Production.ProductCategory`. Products with a NULL subcategory have no category.
* A sales person's name: `Sales.SalesPerson.BusinessEntityID` → `Person.Person.BusinessEntityID`.
* An employee's name: `HumanResources.Employee.BusinessEntityID` → `Person.Person.BusinessEntityID`.
* A vendor's name is `Purchasing.Vendor.Name`; `Purchasing.PurchaseOrderHeader.VendorID` →
  `Purchasing.Vendor.BusinessEntityID`.

## 5. Status codes and flags

| Column | Values |
|---|---|
| `Sales.SalesOrderHeader.Status` | 1 = In process, 2 = Approved, 3 = Backordered, 4 = Rejected, 5 = Shipped, 6 = Cancelled |
| `Purchasing.PurchaseOrderHeader.Status` | 1 = Pending, 2 = Approved, 3 = Rejected, 4 = Complete |
| `HumanResources.EmployeePayHistory.PayFrequency` | 1 = paid monthly, 2 = paid every two weeks |
| `HumanResources.Employee.SalariedFlag` | 1 = salaried, 0 = hourly |
| `HumanResources.Employee.CurrentFlag` | 1 = currently employed |
| `Production.Product.MakeFlag` | 1 = made in-house, 0 = purchased |
| `Production.Product.FinishedGoodsFlag` | 1 = sellable product, 0 = part or component |

## 6. Never confuse these

* `OrderDate` (placed) vs `ShipDate` (sent) vs `DueDate` (promised) on `Sales.SalesOrderHeader`.
  A late shipment means `ShipDate > DueDate`.
* `SubTotal` (revenue) vs `TotalDue` (incl. tax and freight). Use `SubTotal` for "sales" or
  "revenue" unless the user asks for the amount including tax.
* `ListPrice` (current catalogue price) vs `UnitPrice` (price actually charged on an order line).
* `StandardCost` (cost to make or buy) vs `ListPrice` (selling price).
* An employee's CURRENT department is the `EmployeeDepartmentHistory` row with `EndDate IS NULL`.
  Their CURRENT pay rate is the `EmployeePayHistory` row with the latest `RateChangeDate`.
* Manufacturing: a work order is late when `Production.WorkOrder.EndDate > DueDate`.
  `ScrappedQty` is the number of units scrapped.

## 7. Not yet defined

* There is no agreed definition of a "loyal" or "high-value" customer. If asked, rank by revenue
  and say that no official threshold exists.
