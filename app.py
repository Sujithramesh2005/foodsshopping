import os
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import razorpay
import qrcode as qr_module
from io import BytesIO
import base64

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SESSION_SECRET', 'raamer-foods-secret-key-2024')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///raamerfoods.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

from models import db, User, Product, Category, Order, OrderItem, CartItem

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'admin_login'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route('/')
def index():
    categories = Category.query.all()
    latest_products = Product.query.order_by(Product.id.desc()).limit(10).all()
    trending_products = Product.query.filter_by(is_trending=True).limit(8).all()
    cart_count = get_cart_count()
    return render_template('index.html', categories=categories, latest_products=latest_products, 
                         trending_products=trending_products, cart_count=cart_count)

@app.route('/products')
@app.route('/products/<category_name>')
def products(category_name=None):
    cart_count = get_cart_count()
    if category_name:
        category = Category.query.filter_by(name=category_name).first()
        if category:
            products = Product.query.filter_by(category_id=category.id).all()
        else:
            products = Product.query.all()
    else:
        products = Product.query.all()
    
    categories = Category.query.all()
    return render_template('products.html', products=products, categories=categories, 
                         selected_category=category_name, cart_count=cart_count)

@app.route('/product/<int:product_id>')
def product_detail(product_id):
    product = Product.query.get_or_404(product_id)
    cart_count = get_cart_count()
    return render_template('product_detail.html', product=product, cart_count=cart_count)

@app.route('/add_to_cart/<int:product_id>', methods=['POST'])
def add_to_cart(product_id):
    product = Product.query.get_or_404(product_id)
    quantity = int(request.form.get('quantity', 1))
    
    session_id = get_or_create_session_id()
    
    cart_item = CartItem.query.filter_by(session_id=session_id, product_id=product_id).first()
    
    if cart_item:
        cart_item.quantity += quantity
    else:
        cart_item = CartItem(session_id=session_id, product_id=product_id, quantity=quantity)
        db.session.add(cart_item)
    
    db.session.commit()
    flash(f'{product.name} added to cart!', 'success')
    return redirect(url_for('products'))

@app.route('/cart')
def cart():
    session_id = get_or_create_session_id()
    cart_items = CartItem.query.filter_by(session_id=session_id).all()
    
    total = sum(item.product.price * item.quantity for item in cart_items)
    cart_count = len(cart_items)
    
    return render_template('cart.html', cart_items=cart_items, total=total, cart_count=cart_count)

@app.route('/update_cart/<int:item_id>', methods=['POST'])
def update_cart(item_id):
    cart_item = CartItem.query.get_or_404(item_id)
    quantity = int(request.form.get('quantity', 1))
    
    if quantity > 0:
        cart_item.quantity = quantity
        db.session.commit()
        flash('Cart updated!', 'success')
    else:
        db.session.delete(cart_item)
        db.session.commit()
        flash('Item removed from cart!', 'info')
    
    return redirect(url_for('cart'))

@app.route('/remove_from_cart/<int:item_id>')
def remove_from_cart(item_id):
    cart_item = CartItem.query.get_or_404(item_id)
    db.session.delete(cart_item)
    db.session.commit()
    flash('Item removed from cart!', 'info')
    return redirect(url_for('cart'))

@app.route('/checkout')
def checkout():
    session_id = get_or_create_session_id()
    cart_items = CartItem.query.filter_by(session_id=session_id).all()
    
    if not cart_items:
        flash('Your cart is empty!', 'warning')
        return redirect(url_for('products'))
    
    total = sum(item.product.price * item.quantity for item in cart_items)
    cart_count = len(cart_items)
    
    upi_id = "raamerfood@paytm"
    qr_code_data = generate_upi_qr(upi_id, total)
    
    return render_template('checkout.html', cart_items=cart_items, total=total, 
                         cart_count=cart_count, upi_id=upi_id, qr_code=qr_code_data)

@app.route('/create_order', methods=['POST'])
def create_order():
    session_id = get_or_create_session_id()
    cart_items = CartItem.query.filter_by(session_id=session_id).all()
    
    if not cart_items:
        return jsonify({'error': 'Cart is empty'}), 400
    
    customer_name = request.form.get('customer_name')
    customer_email = request.form.get('customer_email')
    customer_phone = request.form.get('customer_phone')
    customer_address = request.form.get('customer_address')
    payment_method = request.form.get('payment_method')
    
    total = sum(item.product.price * item.quantity for item in cart_items)
    
    order = Order(
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        customer_address=customer_address,
        total_amount=total,
        payment_method=payment_method,
        status='Pending Payment'
    )
    db.session.add(order)
    db.session.flush()
    
    for cart_item in cart_items:
        order_item = OrderItem(
            order_id=order.id,
            product_id=cart_item.product_id,
            quantity=cart_item.quantity,
            price=cart_item.product.price
        )
        db.session.add(order_item)
        db.session.delete(cart_item)
    
    db.session.commit()
    
    if payment_method == 'razorpay':
        razorpay_key = os.environ.get('RAZORPAY_KEY_ID', 'test_key')
        razorpay_secret = os.environ.get('RAZORPAY_KEY_SECRET', 'test_secret')
        
        client = razorpay.Client(auth=(razorpay_key, razorpay_secret))
        
        payment_data = {
            'amount': int(total * 100),
            'currency': 'INR',
            'receipt': f'order_{order.id}',
            'notes': {
                'order_id': order.id,
                'customer_name': customer_name
            }
        }
        
        try:
            razorpay_order = client.order.create(data=payment_data)
            order.razorpay_order_id = razorpay_order['id']
            db.session.commit()
            
            return jsonify({
                'success': True,
                'razorpay_order_id': razorpay_order['id'],
                'razorpay_key': razorpay_key,
                'amount': int(total * 100),
                'order_id': order.id
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    else:
        order.status = 'Payment Pending (UPI)'
        db.session.commit()
        return jsonify({'success': True, 'order_id': order.id, 'redirect': url_for('order_success', order_id=order.id)})

@app.route('/payment_callback', methods=['POST'])
def payment_callback():
    payment_id = request.form.get('razorpay_payment_id')
    order_id = request.form.get('razorpay_order_id')
    signature = request.form.get('razorpay_signature')
    
    order = Order.query.filter_by(razorpay_order_id=order_id).first()
    
    if order:
        order.razorpay_payment_id = payment_id
        order.razorpay_signature = signature
        order.status = 'Payment Received'
        db.session.commit()
        
        return redirect(url_for('order_success', order_id=order.id))
    
    return redirect(url_for('index'))

@app.route('/order_success/<int:order_id>')
def order_success(order_id):
    order = Order.query.get_or_404(order_id)
    return render_template('order_success.html', order=order)

@app.route('/orders')
def orders():
    email = request.args.get('email')
    phone = request.args.get('phone')
    
    if email:
        customer_orders = Order.query.filter_by(customer_email=email).order_by(Order.created_at.desc()).all()
    elif phone:
        customer_orders = Order.query.filter_by(customer_phone=phone).order_by(Order.created_at.desc()).all()
    else:
        customer_orders = []
    
    cart_count = get_cart_count()
    return render_template('orders.html', orders=customer_orders, cart_count=cart_count)

@app.route('/about')
def about():
    cart_count = get_cart_count()
    return render_template('about.html', cart_count=cart_count)

@app.route('/contact')
def contact():
    cart_count = get_cart_count()
    return render_template('contact.html', cart_count=cart_count)

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if current_user.is_authenticated:
        return redirect(url_for('admin_dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Invalid username or password', 'danger')
    
    return render_template('admin_login.html')

@app.route('/admin/logout')
@login_required
def admin_logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    total_orders = Order.query.count()
    total_products = Product.query.count()
    pending_orders = Order.query.filter(Order.status.contains('Pending')).count()
    total_revenue = db.session.query(db.func.sum(Order.total_amount)).filter(Order.status.contains('Payment Received')).scalar() or 0
    
    recent_orders = Order.query.order_by(Order.created_at.desc()).limit(10).all()
    
    return render_template('admin_dashboard.html', total_orders=total_orders, 
                         total_products=total_products, pending_orders=pending_orders,
                         total_revenue=total_revenue, recent_orders=recent_orders)

@app.route('/admin/products')
@login_required
def admin_products():
    products = Product.query.all()
    categories = Category.query.all()
    return render_template('admin_products.html', products=products, categories=categories)

@app.route('/admin/product/add', methods=['POST'])
@login_required
def admin_add_product():
    name = request.form.get('name')
    description = request.form.get('description')
    price = float(request.form.get('price'))
    category_id = int(request.form.get('category_id'))
    image_url = request.form.get('image_url')
    stock = int(request.form.get('stock', 100))
    is_trending = request.form.get('is_trending') == 'on'
    
    product = Product(name=name, description=description, price=price, 
                     category_id=category_id, image_url=image_url, 
                     stock=stock, is_trending=is_trending)
    db.session.add(product)
    db.session.commit()
    
    flash('Product added successfully!', 'success')
    return redirect(url_for('admin_products'))

@app.route('/admin/product/edit/<int:product_id>', methods=['POST'])
@login_required
def admin_edit_product(product_id):
    product = Product.query.get_or_404(product_id)
    
    product.name = request.form.get('name')
    product.description = request.form.get('description')
    product.price = float(request.form.get('price'))
    product.category_id = int(request.form.get('category_id'))
    product.image_url = request.form.get('image_url')
    product.stock = int(request.form.get('stock'))
    product.is_trending = request.form.get('is_trending') == 'on'
    
    db.session.commit()
    
    flash('Product updated successfully!', 'success')
    return redirect(url_for('admin_products'))

@app.route('/admin/product/delete/<int:product_id>')
@login_required
def admin_delete_product(product_id):
    product = Product.query.get_or_404(product_id)
    db.session.delete(product)
    db.session.commit()
    
    flash('Product deleted successfully!', 'info')
    return redirect(url_for('admin_products'))

@app.route('/admin/orders')
@login_required
def admin_orders():
    orders = Order.query.order_by(Order.created_at.desc()).all()
    return render_template('admin_orders.html', orders=orders)

@app.route('/admin/order/<int:order_id>')
@login_required
def admin_order_detail(order_id):
    order = Order.query.get_or_404(order_id)
    return render_template('admin_order_detail.html', order=order)

@app.route('/admin/order/update_status/<int:order_id>', methods=['POST'])
@login_required
def admin_update_order_status(order_id):
    order = Order.query.get_or_404(order_id)
    new_status = request.form.get('status')
    order.status = new_status
    db.session.commit()
    
    flash('Order status updated!', 'success')
    return redirect(url_for('admin_order_detail', order_id=order_id))

def get_or_create_session_id():
    if 'session_id' not in session:
        import uuid
        session['session_id'] = str(uuid.uuid4())
    return session['session_id']

def get_cart_count():
    session_id = session.get('session_id')
    if session_id:
        return CartItem.query.filter_by(session_id=session_id).count()
    return 0

def generate_upi_qr(upi_id, amount):
    upi_url = f"upi://pay?pa={upi_id}&pn=Raamer Foods&am={amount}&cu=INR"
    
    qr = qr_module.QRCode(version=1, box_size=10, border=5)
    qr.add_data(upi_url)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode()
    
    return f"data:image/png;base64,{img_str}"

def init_db():
    with app.app_context():
        db.create_all()
        
        if User.query.count() == 0:
            admin = User(username='admin', email='rlsujithr@gmail.com')
            admin.set_password('admin123')
            db.session.add(admin)
        
        if Category.query.count() == 0:
            categories = [
                Category(name='Vermicelli', description='Fresh vermicelli and samiya products'),
                Category(name='Noodles', description='Delicious noodles varieties'),
                Category(name='Flour', description='Quality flour products'),
                Category(name='Instant Products', description='Ready to cook instant products'),
                Category(name='Rava', description='Premium quality rava')
            ]
            for cat in categories:
                db.session.add(cat)
            db.session.commit()
        
        if Product.query.count() == 0:
            vermicelli_cat = Category.query.filter_by(name='Vermicelli').first()
            noodles_cat = Category.query.filter_by(name='Noodles').first()
            flour_cat = Category.query.filter_by(name='Flour').first()
            instant_cat = Category.query.filter_by(name='Instant Products').first()
            rava_cat = Category.query.filter_by(name='Rava').first()
            
            products = [
                Product(name='Vermicelli 200g', description='Premium quality vermicelli', price=25, category_id=vermicelli_cat.id, 
                       image_url='https://via.placeholder.com/300x300?text=Vermicelli+200g', stock=100, is_trending=True),
                Product(name='Vermicelli 500g', description='Premium quality vermicelli', price=58, category_id=vermicelli_cat.id,
                       image_url='https://via.placeholder.com/300x300?text=Vermicelli+500g', stock=100, is_trending=True),
                Product(name='Noodles with Masala 200g', description='Tasty noodles with masala', price=30, category_id=noodles_cat.id,
                       image_url='https://via.placeholder.com/300x300?text=Noodles+200g', stock=100, is_trending=True),
                Product(name='Noodles with Masala 100g', description='Tasty noodles with masala', price=14, category_id=noodles_cat.id,
                       image_url='https://via.placeholder.com/300x300?text=Noodles+100g', stock=100),
                Product(name='Roasted Sooji 500g', description='Premium roasted sooji', price=36, category_id=rava_cat.id,
                       image_url='https://via.placeholder.com/300x300?text=Sooji+500g', stock=100, is_trending=True),
                Product(name='Roasted Sooji 250g', description='Premium roasted sooji', price=20, category_id=rava_cat.id,
                       image_url='https://via.placeholder.com/300x300?text=Sooji+250g', stock=100),
                Product(name='Maida 500g', description='Fine quality maida flour', price=35, category_id=flour_cat.id,
                       image_url='https://via.placeholder.com/300x300?text=Maida+500g', stock=100),
                Product(name='Instant Parotta 200g', description='Ready to cook parotta', price=30, category_id=instant_cat.id,
                       image_url='https://via.placeholder.com/300x300?text=Parotta+200g', stock=100),
                Product(name='Ragi Vermicelli 200g', description='Healthy ragi vermicelli', price=30, category_id=vermicelli_cat.id,
                       image_url='https://via.placeholder.com/300x300?text=Ragi+Vermicelli', stock=100, is_trending=True),
                Product(name='Millet Noodles 200g', description='Nutritious millet noodles', price=60, category_id=noodles_cat.id,
                       image_url='https://via.placeholder.com/300x300?text=Millet+Noodles', stock=100, is_trending=True)
            ]
            for prod in products:
                db.session.add(prod)
        
        db.session.commit()
        print("Database initialized successfully!")

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)
