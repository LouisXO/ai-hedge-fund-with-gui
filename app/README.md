# AI Hedge Fund Web Application

A comprehensive web application for AI-powered hedge fund trading analysis with real-time portfolio monitoring and intelligent decision-making.

## 🚀 Features

### 📊 Dashboard
- **AI Trading Analysis**: Configure and run AI-powered trading analysis with multiple analyst agents
- **Real-time Updates**: Live streaming updates during analysis execution
- **Results History**: Save and review previous analysis results
- **Agent Decisions**: Detailed view of each AI agent's trading recommendations with confidence scores and reasoning

### 💼 Portfolio Management
- **Real-time Portfolio Monitoring**: Live portfolio data with auto-refresh capabilities
- **Holdings Overview**: Detailed view of all portfolio positions with P&L tracking
- **Performance Metrics**: Portfolio performance across different time periods
- **Smart Alerts**: Automated alerts for significant losses, concentration risks, and market updates

### 🔄 Agent Flow Visualization
- **Interactive Flow Diagram**: Visual representation of AI agent workflows
- **Real-time Agent Status**: Monitor agent execution and decision-making process

## 🛠️ Technology Stack

### Frontend
- **React 18** with TypeScript
- **Tailwind CSS** for styling
- **Vite** for fast development and building
- **React Flow** for interactive diagrams
- **shadcn/ui** components for consistent UI

### Backend
- **FastAPI** for high-performance API
- **Python 3.8+** with async support
- **Streaming responses** for real-time updates
- **CORS enabled** for frontend integration

## 🏃‍♂️ Quick Start

### Prerequisites
- Node.js 18+ and pnpm
- Python 3.8+ and Poetry
- Git

### 1. Clone and Setup
```bash
git clone <repository-url>
cd ai-hedge-fund
```

### 2. Environment Configuration
```bash
cp .env.example .env
# Edit .env with your API keys (OpenAI, Anthropic, etc.)
```

### 3. Start the Application

#### Option A: Using Setup Scripts (Recommended)
```bash
# Mac/Linux
./app/run.sh

# Windows
./app/run.bat
```

#### Option B: Manual Setup
```bash
# Backend
cd app/backend
pip install -r requirements.txt
uvicorn app.backend.main:app --reload --port 8000

# Frontend (new terminal)
cd app/frontend
npm install
npm run dev
```

### 4. Access the Application
- **Frontend**: http://localhost:5173
- **Backend API**: http://localhost:8000
- **API Documentation**: http://localhost:8000/docs

## 📱 Using the Web App

### Dashboard
1. **Configure Analysis**:
   - Enter stock tickers (comma-separated)
   - Set date range (optional)
   - Configure portfolio settings
   - Select AI analysts and model
   - Choose whether to show detailed reasoning

2. **Run Analysis**:
   - Click "Run AI Analysis"
   - Watch real-time updates in the Live Updates panel
   - View results as they come in

3. **Review Results**:
   - See agent decisions with confidence scores
   - Read detailed reasoning for each recommendation
   - View decision summary and statistics
   - Access previous analysis from history

### Portfolio Dashboard
1. **Monitor Portfolio**:
   - View real-time portfolio value and P&L
   - Track individual holdings performance
   - Monitor portfolio allocation and weights

2. **Configure Auto-refresh**:
   - Toggle auto-refresh on/off
   - Set refresh interval (10s, 30s, 1m, 5m)
   - Monitor live status indicator

3. **Review Alerts**:
   - Check for significant losses
   - Monitor concentration risks
   - Stay updated with market insights

## 🔧 API Endpoints

### Hedge Fund Analysis
- `POST /api/v1/hedge-fund/run` - Run AI trading analysis (streaming)

### Portfolio Management
- `GET /api/v1/portfolio/` - Get complete portfolio data
- `GET /api/v1/portfolio/holdings` - Get holdings only
- `GET /api/v1/portfolio/performance` - Get performance metrics
- `GET /api/v1/portfolio/alerts` - Get portfolio alerts

### Health Check
- `GET /api/v1/health` - API health status

## 🎯 Key Features Explained

### AI Analyst Agents
- **Financial Analyst**: Fundamental analysis and financial metrics
- **Technical Analyst**: Chart patterns and technical indicators  
- **Sentiment Analyst**: Market sentiment and news analysis
- **Risk Analyst**: Risk assessment and portfolio analysis

### Real-time Updates
The application uses Server-Sent Events (SSE) for real-time streaming:
- Progress updates during analysis
- Live portfolio data refresh
- Instant alert notifications

### Data Persistence
- Analysis results saved to localStorage
- Portfolio history tracking
- User preferences persistence

## 🔒 Security & Configuration

### Environment Variables
```bash
# AI Model API Keys
OPENAI_API_KEY=your_openai_key
ANTHROPIC_API_KEY=your_anthropic_key

# Database (if using)
DATABASE_URL=your_database_url

# CORS Settings
FRONTEND_URL=http://localhost:5173
```

### CORS Configuration
The backend is configured to allow requests from:
- http://localhost:5173 (development)
- http://127.0.0.1:5173 (alternative localhost)

## 🚀 Deployment

### Development
```bash
# Frontend
npm run dev

# Backend  
uvicorn app.backend.main:app --reload
```

### Production
```bash
# Frontend
npm run build
npm run preview

# Backend
uvicorn app.backend.main:app --host 0.0.0.0 --port 8000
```

### Docker (Coming Soon)
```bash
docker-compose up --build
```

## 🧪 Testing

### Frontend
```bash
cd app/frontend
npm run test
```

### Backend
```bash
cd app/backend
pytest
```

## 📈 Performance Optimization

### Frontend
- Component lazy loading
- Efficient re-rendering with React hooks
- Optimized bundle size with Vite
- Responsive design for all devices

### Backend
- Async/await for non-blocking operations
- Streaming responses for real-time data
- Efficient data processing
- Connection pooling for databases

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

See [CONTRIBUTING.md](../CONTRIBUTING.md) for detailed guidelines.

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](../LICENSE) file for details.

## ⚠️ Disclaimer

This application is for educational and research purposes only. It should not be used for actual trading without proper risk management and compliance with financial regulations. Always consult with financial professionals before making investment decisions.

## 🆘 Support

- **Issues**: GitHub Issues
- **Discussions**: GitHub Discussions
- **Documentation**: See `/docs` folder

---

**Happy Trading! 🚀📈**
