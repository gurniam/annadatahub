// AnnadataHub API Connector
// Railway Backend URL
const BACKEND = "https://annadatahub-production-9db.up.railway.app";

// ── AUTH FUNCTIONS ──────────────────────────────────────────────
const AnnadataAPI = {

  // Get stored token
  getToken: () => {
    try { return localStorage.getItem("annadatahub_token"); } catch(e) { return null; }
  },

  // Get stored user
  getUser: () => {
    try { return JSON.parse(localStorage.getItem("annadatahub_user") || "null"); } catch(e) { return null; }
  },

  // Save login
  saveLogin: (token, user) => {
    try {
      localStorage.setItem("annadatahub_token", token);
      localStorage.setItem("annadatahub_user", JSON.stringify(user));
    } catch(e) {}
  },

  // Logout
  logout: () => {
    try {
      localStorage.removeItem("annadatahub_token");
      localStorage.removeItem("annadatahub_user");
    } catch(e) {}
    window.location.href = "index.html";
  },

  // Register farmer
  register: async (email, password, fullName, phone, state) => {
    const res = await fetch(`${BACKEND}/api/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, full_name: fullName, phone, state })
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Registration failed");
    }
    return res.json();
  },

  // Login farmer
  login: async (email, password) => {
    const res = await fetch(`${BACKEND}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password })
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Login failed");
    }
    return res.json();
  },

  // Get mandi prices
  getMandiPrices: async (crop, state) => {
    const res = await fetch(`${BACKEND}/api/mandi/prices?crop=${crop}&state=${state}`);
    return res.json();
  },

  // Get weather
  getWeather: async (location) => {
    const res = await fetch(`${BACKEND}/api/weather?location=${location}`);
    return res.json();
  },

  // Get govt schemes
  getSchemes: async (state) => {
    const res = await fetch(`${BACKEND}/api/schemes?state=${state}`);
    return res.json();
  },

  // Get MSP
  getMSP: async (crop) => {
    const res = await fetch(`${BACKEND}/api/msp?crop=${crop}`);
    return res.json();
  },

  // Scan crop
  scanCrop: async (imageBase64, cropType, language) => {
    const token = AnnadataAPI.getToken();
    const res = await fetch(`${BACKEND}/api/crop/scan`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { "Authorization": `Bearer ${token}` } : {})
      },
      body: JSON.stringify({ image_base64: imageBase64, crop_type: cropType, language })
    });
    return res.json();
  },

  // Ask AI
  askAI: async (question, language) => {
    const res = await fetch(`${BACKEND}/api/ai/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, language })
    });
    return res.json();
  },

  // Get user profile
  getProfile: async () => {
    const token = AnnadataAPI.getToken();
    if (!token) throw new Error("Not logged in");
    const res = await fetch(`${BACKEND}/api/user/profile`, {
      headers: { "Authorization": `Bearer ${token}` }
    });
    return res.json();
  },

  // FarmGram posts
  getPosts: async () => {
    const res = await fetch(`${BACKEND}/api/farmgram/posts`);
    return res.json();
  },

  // Create FarmGram post
  createPost: async (content, cropType, location) => {
    const token = AnnadataAPI.getToken();
    if (!token) throw new Error("Please login first");
    const res = await fetch(`${BACKEND}/api/farmgram/post`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${token}`
      },
      body: JSON.stringify({ content, crop_type: cropType, location })
    });
    return res.json();
  },

  // Check if logged in
  isLoggedIn: () => {
    return !!AnnadataAPI.getToken();
  }
};

// ── LOGIN MODAL ──────────────────────────────────────────────────
function showLoginModal() {
  if (document.getElementById("annadataLoginModal")) return;

  const modal = document.createElement("div");
  modal.id = "annadataLoginModal";
  modal.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:flex-end;justify-content:center;font-family:'Hind',sans-serif";
  modal.innerHTML = `
    <div style="background:white;border-radius:20px 20px 0 0;padding:24px;width:100%;max-width:480px">
      <div style="font-family:'Baloo 2',cursive;font-size:1.2rem;font-weight:800;color:#1a5c2e;margin-bottom:4px;text-align:center">🌾 Login to AnnadataHub</div>
      <div style="font-size:0.82rem;color:#6b7c6b;text-align:center;margin-bottom:16px">Save your scans and access all features</div>
      
      <div id="loginForm">
        <div style="margin-bottom:10px">
          <label style="font-size:0.78rem;font-weight:600;color:#6b7c6b;display:block;margin-bottom:3px">Email</label>
          <input id="loginEmail" type="email" placeholder="yourname@gmail.com" style="width:100%;padding:10px 12px;border:1.5px solid #d0e4d4;border-radius:10px;font-size:0.88rem;outline:none;box-sizing:border-box"/>
        </div>
        <div style="margin-bottom:14px">
          <label style="font-size:0.78rem;font-weight:600;color:#6b7c6b;display:block;margin-bottom:3px">Password</label>
          <input id="loginPassword" type="password" placeholder="Your password" style="width:100%;padding:10px 12px;border:1.5px solid #d0e4d4;border-radius:10px;font-size:0.88rem;outline:none;box-sizing:border-box"/>
        </div>
        <div id="loginError" style="background:#fee2e2;color:#dc2626;padding:8px 12px;border-radius:8px;font-size:0.82rem;margin-bottom:10px;display:none"></div>
        <button onclick="doLogin()" style="width:100%;padding:12px;background:#1a5c2e;color:white;border:none;border-radius:12px;font-family:'Baloo 2',cursive;font-size:1rem;font-weight:700;cursor:pointer">Login →</button>
        <div style="text-align:center;margin:10px 0;font-size:0.82rem;color:#6b7c6b">Don't have account?</div>
        <button onclick="showRegisterForm()" style="width:100%;padding:12px;background:#e8f5ec;color:#1a5c2e;border:none;border-radius:12px;font-family:'Baloo 2',cursive;font-size:1rem;font-weight:700;cursor:pointer">Create Free Account</button>
        <button onclick="closeLoginModal()" style="width:100%;padding:10px;background:none;border:none;color:#6b7c6b;font-size:0.82rem;cursor:pointer;margin-top:6px">Continue without login</button>
      </div>

      <div id="registerForm" style="display:none">
        <div style="margin-bottom:10px">
          <label style="font-size:0.78rem;font-weight:600;color:#6b7c6b;display:block;margin-bottom:3px">Full Name</label>
          <input id="regName" type="text" placeholder="Your full name" style="width:100%;padding:10px 12px;border:1.5px solid #d0e4d4;border-radius:10px;font-size:0.88rem;outline:none;box-sizing:border-box"/>
        </div>
        <div style="margin-bottom:10px">
          <label style="font-size:0.78rem;font-weight:600;color:#6b7c6b;display:block;margin-bottom:3px">Email</label>
          <input id="regEmail" type="email" placeholder="yourname@gmail.com" style="width:100%;padding:10px 12px;border:1.5px solid #d0e4d4;border-radius:10px;font-size:0.88rem;outline:none;box-sizing:border-box"/>
        </div>
        <div style="margin-bottom:10px">
          <label style="font-size:0.78rem;font-weight:600;color:#6b7c6b;display:block;margin-bottom:3px">Phone</label>
          <input id="regPhone" type="tel" placeholder="9876543210" style="width:100%;padding:10px 12px;border:1.5px solid #d0e4d4;border-radius:10px;font-size:0.88rem;outline:none;box-sizing:border-box"/>
        </div>
        <div style="margin-bottom:10px">
          <label style="font-size:0.78rem;font-weight:600;color:#6b7c6b;display:block;margin-bottom:3px">State</label>
          <select id="regState" style="width:100%;padding:10px 12px;border:1.5px solid #d0e4d4;border-radius:10px;font-size:0.88rem;outline:none;box-sizing:border-box">
            <option>Punjab</option><option>Haryana</option><option>Uttar Pradesh</option>
            <option>Maharashtra</option><option>Madhya Pradesh</option><option>Gujarat</option>
            <option>Rajasthan</option><option>Bihar</option><option>West Bengal</option>
            <option>Andhra Pradesh</option><option>Tamil Nadu</option><option>Karnataka</option>
            <option>Kerala</option><option>Telangana</option><option>Other</option>
          </select>
        </div>
        <div style="margin-bottom:14px">
          <label style="font-size:0.78rem;font-weight:600;color:#6b7c6b;display:block;margin-bottom:3px">Password (min 6 characters)</label>
          <input id="regPassword" type="password" placeholder="Create password" style="width:100%;padding:10px 12px;border:1.5px solid #d0e4d4;border-radius:10px;font-size:0.88rem;outline:none;box-sizing:border-box"/>
        </div>
        <div id="regError" style="background:#fee2e2;color:#dc2626;padding:8px 12px;border-radius:8px;font-size:0.82rem;margin-bottom:10px;display:none"></div>
        <button onclick="doRegister()" style="width:100%;padding:12px;background:#1a5c2e;color:white;border:none;border-radius:12px;font-family:'Baloo 2',cursive;font-size:1rem;font-weight:700;cursor:pointer">Create Free Account 🌾</button>
        <button onclick="showLoginForm()" style="width:100%;padding:10px;background:none;border:none;color:#6b7c6b;font-size:0.82rem;cursor:pointer;margin-top:6px">Already have account? Login</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
}

function closeLoginModal() {
  const m = document.getElementById("annadataLoginModal");
  if (m) m.remove();
}

function showRegisterForm() {
  document.getElementById("loginForm").style.display = "none";
  document.getElementById("registerForm").style.display = "block";
}

function showLoginForm() {
  document.getElementById("registerForm").style.display = "none";
  document.getElementById("loginForm").style.display = "block";
}

async function doLogin() {
  const email = document.getElementById("loginEmail").value;
  const password = document.getElementById("loginPassword").value;
  const errDiv = document.getElementById("loginError");
  if (!email || !password) {
    errDiv.textContent = "Please enter email and password";
    errDiv.style.display = "block";
    return;
  }
  try {
    const data = await AnnadataAPI.login(email, password);
    AnnadataAPI.saveLogin(data.token, data.user);
    closeLoginModal();
    updateNavbar();
    showToastGlobal("Welcome back " + data.user.full_name + "! 🌾");
  } catch(e) {
    errDiv.textContent = e.message;
    errDiv.style.display = "block";
  }
}

async function doRegister() {
  const name = document.getElementById("regName").value;
  const email = document.getElementById("regEmail").value;
  const phone = document.getElementById("regPhone").value;
  const state = document.getElementById("regState").value;
  const password = document.getElementById("regPassword").value;
  const errDiv = document.getElementById("regError");
  if (!name || !email || !password) {
    errDiv.textContent = "Please fill all required fields";
    errDiv.style.display = "block";
    return;
  }
  if (password.length < 6) {
    errDiv.textContent = "Password must be at least 6 characters";
    errDiv.style.display = "block";
    return;
  }
  try {
    const data = await AnnadataAPI.register(email, password, name, phone, state);
    AnnadataAPI.saveLogin(data.token, data.user);
    closeLoginModal();
    updateNavbar();
    showToastGlobal("Welcome to AnnadataHub " + data.user.full_name + "! 🌾");
  } catch(e) {
    errDiv.textContent = e.message;
    errDiv.style.display = "block";
  }
}

function updateNavbar() {
  const user = AnnadataAPI.getUser();
  const loginBtns = document.querySelectorAll(".login-btn");
  const userBtns = document.querySelectorAll(".user-btn");
  const userNames = document.querySelectorAll(".user-name");
  if (user) {
    loginBtns.forEach(b => b.style.display = "none");
    userBtns.forEach(b => b.style.display = "flex");
    userNames.forEach(b => b.textContent = user.full_name.split(" ")[0]);
  } else {
    loginBtns.forEach(b => b.style.display = "flex");
    userBtns.forEach(b => b.style.display = "none");
  }
}

function showToastGlobal(msg) {
  let toast = document.getElementById("globalToast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "globalToast";
    toast.style.cssText = "position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:#1a5c2e;color:white;padding:10px 20px;border-radius:100px;font-size:0.83rem;z-index:9998;white-space:nowrap;max-width:90vw;text-align:center;font-family:'Hind',sans-serif";
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.style.display = "block";
  setTimeout(() => { toast.style.display = "none"; }, 3000);
}

// Initialize on page load
document.addEventListener("DOMContentLoaded", () => {
  updateNavbar();
});
