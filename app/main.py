import os
from datetime import datetime, timedelta
from typing import Literal

from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from pwdlib import PasswordHash
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import init_db, get_db
from app.models import User, Category, Supplier, Product, StockTransaction

SECRET_KEY = os.getenv("JWT_SECRET", "dev-secret-change-before-deployment")
ALGORITHM = "HS256"
password_hash = PasswordHash.recommended()
security = HTTPBearer()

init_db()

app = FastAPI(title="Real-Time Inventory & Stock Management API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConnectionManager:
    def __init__(self):
        self.connections = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.connections:
            self.connections.remove(websocket)

    async def broadcast(self, data: dict):
        for connection in self.connections.copy():
            try:
                await connection.send_json(data)
            except Exception:
                self.disconnect(connection)


manager = ConnectionManager()


class RegisterInput(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=8, max_length=128)


class LoginInput(BaseModel):
    username: str
    password: str


class CategoryInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class SupplierInput(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    phone: str | None = None
    email: str | None = None


class ProductInput(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    sku: str = Field(min_length=1, max_length=80)
    category_id: int | None = None
    supplier_id: int | None = None
    cost_price: float = Field(ge=0)
    selling_price: float = Field(ge=0)
    quantity: int = Field(default=0, ge=0)
    low_stock_limit: int = Field(default=5, ge=0)


class StockInput(BaseModel):
    product_id: int
    transaction_type: Literal["purchase", "sale", "return", "adjustment"]
    quantity: int
    unit_price: float | None = Field(default=None, ge=0)


def current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
    except (JWTError, ValueError, KeyError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


def admin_only(user: User = Depends(current_user)):
    if user.role != "Admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def make_token(user: User):
    expires = datetime.utcnow() + timedelta(hours=12)
    return jwt.encode(
        {"sub": str(user.id), "exp": expires},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def product_data(p: Product):
    return {
        "id": p.id,
        "name": p.name,
        "sku": p.sku,
        "category_id": p.category_id,
        "supplier_id": p.supplier_id,
        "cost_price": p.cost_price,
        "selling_price": p.selling_price,
        "quantity": p.quantity,
        "low_stock_limit": p.low_stock_limit,
        "low_stock": p.quantity <= p.low_stock_limit,
        "out_of_stock": p.quantity == 0,
    }


@app.get("/")
def root():
    return {"message": "Inventory Management API is running", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/auth/register")
def register(data: RegisterInput, db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == data.username).first():
        raise HTTPException(status_code=409, detail="Username already exists")

    # First registered account becomes Admin; later accounts are Staff.
    role = "Admin" if db.query(User).count() == 0 else "Staff"
    user = User(
        username=data.username,
        password_hash=password_hash.hash(data.password),
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "username": user.username, "role": user.role}


@app.post("/auth/login")
def login(data: LoginInput, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if not user or not password_hash.verify(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account inactive")

    return {
        "access_token": make_token(user),
        "token_type": "bearer",
        "role": user.role,
        "username": user.username,
    }


@app.get("/auth/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "username": user.username, "role": user.role}


@app.get("/categories")
def list_categories(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return db.query(Category).order_by(Category.name).all()


@app.post("/categories")
def create_category(
    data: CategoryInput,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    if db.query(Category).filter(Category.name == data.name).first():
        raise HTTPException(status_code=409, detail="Category already exists")
    item = Category(name=data.name)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@app.get("/suppliers")
def list_suppliers(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return db.query(Supplier).order_by(Supplier.name).all()


@app.post("/suppliers")
def create_supplier(
    data: SupplierInput,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    item = Supplier(name=data.name, phone=data.phone, email=data.email)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@app.get("/products")
def list_products(
    search: str | None = None,
    category_id: int | None = None,
    low_stock: bool = False,
    out_of_stock: bool = False,
    sort_by: Literal["name", "quantity", "selling_price"] = "name",
    skip: int = Query(default=0, ge=0),
limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    query = db.query(Product)

    if search:
        query = query.filter(
            (Product.name.ilike(f"%{search}%")) |
            (Product.sku.ilike(f"%{search}%"))
        )
    if category_id is not None:
        query = query.filter(Product.category_id == category_id)
    if out_of_stock:
        query = query.filter(Product.quantity == 0)
    elif low_stock:
        query = query.filter(Product.quantity <= Product.low_stock_limit)

    total = query.count()
    query = query.order_by(getattr(Product, sort_by)).offset(skip).limit(limit)

    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "items": [product_data(p) for p in query.all()],
    }


@app.post("/products")
def create_product(
    data: ProductInput,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    if db.query(Product).filter(Product.sku == data.sku).first():
        raise HTTPException(status_code=409, detail="SKU already exists")
    if data.category_id is not None and not db.get(Category, data.category_id):
        raise HTTPException(status_code=404, detail="Category not found")
    if data.supplier_id is not None and not db.get(Supplier, data.supplier_id):
        raise HTTPException(status_code=404, detail="Supplier not found")

    product = Product(**data.model_dump())
    db.add(product)
    db.commit()
    db.refresh(product)
    return product_data(product)


@app.get("/products/{product_id}")
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product_data(product)


@app.patch("/products/{product_id}")
def update_product(
    product_id: int,
    data: ProductInput,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    duplicate = db.query(Product).filter(
        Product.sku == data.sku, Product.id != product_id
    ).first()
    if duplicate:
        raise HTTPException(status_code=409, detail="SKU already exists")

    for key, value in data.model_dump().items():
        setattr(product, key, value)

    db.commit()
    db.refresh(product)
    return product_data(product)


@app.delete("/products/{product_id}")
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if db.query(StockTransaction).filter(
        StockTransaction.product_id == product_id
    ).first():
        raise HTTPException(
            status_code=409,
            detail="Product has transaction history and cannot be deleted",
        )
    db.delete(product)
    db.commit()
    return {"message": "Product deleted"}


@app.post("/stock/transactions")
async def create_stock_transaction(
    data: StockInput,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    product = db.get(Product, data.product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    if data.transaction_type == "adjustment":
        if data.quantity == 0:
            raise HTTPException(status_code=400, detail="Adjustment cannot be zero")
        delta = data.quantity
        recorded_quantity = data.quantity
    else:
        if data.quantity <= 0:
            raise HTTPException(status_code=400, detail="Quantity must be positive")
        recorded_quantity = data.quantity
        delta = -data.quantity if data.transaction_type == "sale" else data.quantity

    if product.quantity + delta < 0:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient stock. Available quantity: {product.quantity}",
        )

    if data.unit_price is not None:
        price = data.unit_price
    elif data.transaction_type == "purchase":
        price = product.cost_price
    else:
        price = product.selling_price

    product.quantity += delta
    transaction = StockTransaction(
        product_id=product.id,
        user_id=user.id,
        transaction_type=data.transaction_type,
        quantity=recorded_quantity,
        unit_price=price,
    )
    db.add(transaction)
    db.commit()
    db.refresh(transaction)
    db.refresh(product)

    event = {
        "event": "stock_updated",
        "product": product_data(product),
        "transaction": {
            "id": transaction.id,
            "type": transaction.transaction_type,
            "quantity": transaction.quantity,
            "unit_price": transaction.unit_price,
            "created_at": transaction.created_at.isoformat(),
        },
    }
    await manager.broadcast(event)
    return event


@app.get("/transactions")
def transaction_history(
    product_id: int | None = None,
    transaction_type: str | None = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    limit = min(max(limit, 1), 100)
    skip = max(skip, 0)
    query = db.query(StockTransaction)
    if product_id is not None:
        query = query.filter(StockTransaction.product_id == product_id)
    if transaction_type:
        query = query.filter(StockTransaction.transaction_type == transaction_type)

    return [
        {
            "id": t.id,
            "product_id": t.product_id,
            "user_id": t.user_id,
            "transaction_type": t.transaction_type,
            "quantity": t.quantity,
            "unit_price": t.unit_price,
            "created_at": t.created_at.isoformat(),
        }
        for t in query.order_by(StockTransaction.created_at.desc())
        .offset(skip).limit(limit).all()
    ]


@app.get("/analytics/summary")
def analytics_summary(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    products = db.query(Product).all()
    transactions = db.query(StockTransaction).filter(
        StockTransaction.transaction_type == "sale"
    ).all()

    sales_total = sum(t.quantity * t.unit_price for t in transactions)
    estimated_profit = sum(
        t.quantity * (t.unit_price - (db.get(Product, t.product_id).cost_price
        if db.get(Product, t.product_id) else 0))
        for t in transactions
    )

    return {
        "total_products": len(products),
        "total_units": sum(p.quantity for p in products),
        "inventory_value": round(sum(p.quantity * p.cost_price for p in products), 2),
        "low_stock_items": sum(1 for p in products if 0 < p.quantity <= p.low_stock_limit),
        "out_of_stock_items": sum(1 for p in products if p.quantity == 0),
        "sales_total": round(sales_total, 2),
        "estimated_profit": round(estimated_profit, 2),
    }


@app.get("/analytics/transactions-by-type")
def analytics_by_type(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    rows = db.query(
        StockTransaction.transaction_type,
        StockTransaction.quantity,
    ).all()
    result = {}
    for transaction_type, quantity in rows:
        result[transaction_type] = result.get(transaction_type, 0) + quantity
    return result


@app.websocket("/ws/stock")
async def stock_websocket(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive; updates are broadcast after stock transactions.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
