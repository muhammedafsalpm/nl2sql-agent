<!--
  Worked examples for the AdventureWorks sample database (OLTP, e.g. AdventureWorks2022).
-->

Q: How many orders are there?
```sql
SELECT COUNT(*) AS order_count
FROM Sales.SalesOrderHeader;
```

Q: What is the total revenue per year?
```sql
SELECT YEAR(soh.OrderDate) AS order_year,
       SUM(soh.SubTotal)   AS revenue
FROM Sales.SalesOrderHeader soh
GROUP BY YEAR(soh.OrderDate)
ORDER BY order_year;
```

Q: Show revenue by product category.
```sql
-- Sales.SalesOrderDetail: ProductID, LineTotal
-- Production.Product: ProductID, ProductSubcategoryID
-- Production.ProductSubcategory: ProductSubcategoryID, ProductCategoryID
-- Production.ProductCategory: ProductCategoryID, Name
SELECT pc.Name AS category, SUM(sod.LineTotal) AS revenue
FROM Sales.SalesOrderDetail sod
JOIN Production.Product p             ON p.ProductID = sod.ProductID
JOIN Production.ProductSubcategory ps ON ps.ProductSubcategoryID = p.ProductSubcategoryID
JOIN Production.ProductCategory pc    ON pc.ProductCategoryID = ps.ProductCategoryID
GROUP BY pc.Name
ORDER BY revenue DESC;
```

Q: Top 5 products by quantity sold.
```sql
SELECT TOP 5 p.Name AS product, SUM(sod.OrderQty) AS units_sold
FROM Sales.SalesOrderDetail sod
JOIN Production.Product p ON p.ProductID = sod.ProductID
GROUP BY p.Name
ORDER BY units_sold DESC;
```

Q: Revenue by territory, online vs reseller.
```sql
SELECT st.Name AS territory,
       SUM(CASE WHEN soh.OnlineOrderFlag = 1 THEN soh.SubTotal ELSE 0 END) AS online_revenue,
       SUM(CASE WHEN soh.OnlineOrderFlag = 0 THEN soh.SubTotal ELSE 0 END) AS reseller_revenue
FROM Sales.SalesOrderHeader soh
JOIN Sales.SalesTerritory st ON st.TerritoryID = soh.TerritoryID
GROUP BY st.Name
ORDER BY st.Name;
```

Q: Who are the top 10 customers by revenue?
```sql
-- A customer is either a person (online) or a store (reseller).
SELECT TOP 10
       COALESCE(s.Name, pp.FirstName + ' ' + pp.LastName) AS customer,
       SUM(soh.SubTotal) AS revenue
FROM Sales.SalesOrderHeader soh
JOIN Sales.Customer c     ON c.CustomerID = soh.CustomerID
LEFT JOIN Sales.Store s   ON s.BusinessEntityID = c.StoreID
LEFT JOIN Person.Person pp ON pp.BusinessEntityID = c.PersonID
GROUP BY COALESCE(s.Name, pp.FirstName + ' ' + pp.LastName)
ORDER BY revenue DESC;
```

Q: How many orders were shipped late?
```sql
SELECT COUNT(*) AS late_orders
FROM Sales.SalesOrderHeader
WHERE ShipDate > DueDate;
```

Q: Revenue in the last 3 months of data.
```sql
-- Relative periods are measured from the latest order date, not today.
WITH latest AS (SELECT MAX(OrderDate) AS max_date FROM Sales.SalesOrderHeader)
SELECT SUM(soh.SubTotal) AS revenue
FROM Sales.SalesOrderHeader soh
CROSS JOIN latest l
WHERE soh.OrderDate > DATEADD(month, -3, l.max_date);
```

Q: Sales people with their sales this year compared to their quota.
```sql
SELECT pp.FirstName + ' ' + pp.LastName AS sales_person,
       sp.SalesQuota,
       sp.SalesYTD
FROM Sales.SalesPerson sp
JOIN Person.Person pp ON pp.BusinessEntityID = sp.BusinessEntityID
ORDER BY sp.SalesYTD DESC;
```

Q: Which products are low on stock (under 100 units)?
```sql
SELECT p.Name AS product, SUM(pi.Quantity) AS units_in_stock
FROM Production.ProductInventory pi
JOIN Production.Product p ON p.ProductID = pi.ProductID
WHERE p.FinishedGoodsFlag = 1
GROUP BY p.Name
HAVING SUM(pi.Quantity) < 100
ORDER BY units_in_stock;
```

Q: How many employees are in each department right now?
```sql
SELECT d.Name AS department, COUNT(*) AS employees
FROM HumanResources.EmployeeDepartmentHistory edh
JOIN HumanResources.Department d ON d.DepartmentID = edh.DepartmentID
JOIN HumanResources.Employee e   ON e.BusinessEntityID = edh.BusinessEntityID
WHERE edh.EndDate IS NULL AND e.CurrentFlag = 1
GROUP BY d.Name
ORDER BY employees DESC;
```

Q: What is each employee's current pay rate?
```sql
WITH latest_pay AS (
    SELECT eph.BusinessEntityID, eph.Rate,
           ROW_NUMBER() OVER (PARTITION BY eph.BusinessEntityID
                              ORDER BY eph.RateChangeDate DESC) AS rn
    FROM HumanResources.EmployeePayHistory eph
)
SELECT pp.FirstName + ' ' + pp.LastName AS employee, e.JobTitle, lp.Rate AS hourly_rate
FROM latest_pay lp
JOIN HumanResources.Employee e ON e.BusinessEntityID = lp.BusinessEntityID
JOIN Person.Person pp          ON pp.BusinessEntityID = lp.BusinessEntityID
WHERE lp.rn = 1
ORDER BY employee;
```

Q: Which work orders finished after their due date?
```sql
SELECT wo.WorkOrderID, p.Name AS product, wo.DueDate, wo.EndDate,
       DATEDIFF(day, wo.DueDate, wo.EndDate) AS days_late
FROM Production.WorkOrder wo
JOIN Production.Product p ON p.ProductID = wo.ProductID
WHERE wo.EndDate > wo.DueDate
ORDER BY days_late DESC;
```

Q: Total purchase spend per vendor.
```sql
SELECT v.Name AS vendor, SUM(poh.SubTotal) AS spend
FROM Purchasing.PurchaseOrderHeader poh
JOIN Purchasing.Vendor v ON v.BusinessEntityID = poh.VendorID
GROUP BY v.Name
ORDER BY spend DESC;
```
