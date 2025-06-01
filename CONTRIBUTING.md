# Contributing to AI Hedge Fund Web App

Thank you for your interest in contributing to the AI Hedge Fund Web App! This guide will help you get started with development and ensure consistency across the codebase.

## 🚀 Quick Start

1. **Fork the repository** and clone your fork
2. **Install dependencies** using the setup scripts
3. **Create a feature branch** from `main`
4. **Make your changes** following our coding standards
5. **Test your changes** thoroughly
6. **Submit a pull request** with a clear description

## 📋 Development Setup

### Prerequisites

- Node.js 18+ and pnpm
- Python 3.8+ and Poetry
- Docker and Docker Compose
- Git

### Initial Setup

```bash
# Clone your fork
git clone https://github.com/your-username/ai-hedge-fund.git
cd ai-hedge-fund

# Set up environment variables
cp .env.example .env
# Edit .env with your API keys

# Install dependencies (choose one method)
# Option 1: Use setup scripts
./app/run.sh  # Mac/Linux
# or
./app/run.bat  # Windows

# Option 2: Manual setup
cd app
npm install && npm run setup
```

## 🏗️ Project Structure

### Frontend (`app/frontend/`)

```
src/
├── components/          # Reusable UI components
│   ├── ui/             # Basic components (shadcn)
│   ├── charts/         # Chart components
│   ├── forms/          # Form components
│   └── layout/         # Layout components
├── features/           # Feature modules
│   ├── dashboard/      # Main dashboard
│   ├── agents/         # Agent management
│   ├── backtesting/    # Backtesting interface
│   └── portfolio/      # Portfolio management
├── hooks/              # Custom React hooks
├── lib/                # Utility libraries
├── services/           # API services
├── store/              # State management
├── types/              # TypeScript types
└── utils/              # Helper functions
```

### Backend (`app/backend/`)

```
├── models/             # Database models
├── routes/             # API routes
├── services/           # Business logic
├── schemas/            # Pydantic schemas
├── core/               # Core configuration
├── utils/              # Utility functions
├── tests/              # Test files
└── migrations/         # Database migrations
```

## 🎯 Coding Standards

### TypeScript/React Guidelines

#### Component Structure

```typescript
// Use functional components with explicit types
interface AgentCardProps {
  agent: Agent;
  decision: AgentDecision;
  onSelect?: (agentId: string) => void;
}

const AgentCard: React.FC<AgentCardProps> = ({ agent, decision, onSelect }) => {
  // Component logic here
  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg shadow-lg p-6">
      {/* JSX content */}
    </div>
  );
};

export { AgentCard };
export type { AgentCardProps };
```

#### Custom Hooks

```typescript
// Business logic in custom hooks
const useAgentDecisions = (tickers: string[]) => {
  return useQuery({
    queryKey: ["agentDecisions", tickers],
    queryFn: () => fetchAgentDecisions(tickers),
    refetchInterval: 30000,
    enabled: tickers.length > 0,
  });
};
```

#### State Management

```typescript
// Use Zustand for global state
interface AppState {
  selectedTickers: string[];
  showReasoning: boolean;
  setSelectedTickers: (tickers: string[]) => void;
  toggleReasoning: () => void;
}

const useAppStore = create<AppState>((set) => ({
  selectedTickers: [],
  showReasoning: false,
  setSelectedTickers: (tickers) => set({ selectedTickers: tickers }),
  toggleReasoning: () =>
    set((state) => ({
      showReasoning: !state.showReasoning,
    })),
}));
```

### Python/FastAPI Guidelines

#### API Endpoints

```python
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

class AgentDecisionRequest(BaseModel):
    tickers: List[str] = Field(..., min_items=1, max_items=10)
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    show_reasoning: bool = False

@router.post("/decisions", response_model=List[AgentDecisionResponse])
async def get_agent_decisions(
    request: AgentDecisionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> List[AgentDecisionResponse]:
    """Generate trading decisions from all agents."""
    try:
        # Business logic here
        decisions = await decision_service.process_decisions(
            tickers=request.tickers,
            start_date=request.start_date,
            end_date=request.end_date,
            show_reasoning=request.show_reasoning
        )
        return decisions
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

#### Database Models

```python
from sqlalchemy import Column, Integer, String, DateTime, Float, Text
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class AgentDecision(Base):
    __tablename__ = "agent_decisions"

    id = Column(Integer, primary_key=True, index=True)
    agent_name = Column(String(100), nullable=False)
    ticker = Column(String(10), nullable=False)
    recommendation = Column(String(10), nullable=False)  # BUY, SELL, HOLD
    confidence = Column(Float, nullable=False)
    reasoning = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
```

## 🎨 UI/UX Guidelines

### Design System

- **Colors**: Use Tailwind CSS color palette with custom financial theme
- **Typography**: Clear hierarchy with readable fonts
- **Spacing**: 4px grid system (space-1, space-2, etc.)
- **Components**: Consistent component patterns

### Styling Standards

```typescript
// Use Tailwind CSS classes with consistent patterns
const Button = ({ variant = "primary", size = "md", children, ...props }) => {
  const baseClasses =
    "inline-flex items-center justify-center rounded-md font-medium transition-colors";

  const variantClasses = {
    primary: "bg-blue-600 text-white hover:bg-blue-700",
    secondary: "bg-gray-200 text-gray-900 hover:bg-gray-300",
    danger: "bg-red-600 text-white hover:bg-red-700",
  };

  const sizeClasses = {
    sm: "px-3 py-2 text-sm",
    md: "px-4 py-2",
    lg: "px-6 py-3 text-lg",
  };

  return (
    <button
      className={`${baseClasses} ${variantClasses[variant]} ${sizeClasses[size]}`}
      {...props}
    >
      {children}
    </button>
  );
};
```

### Accessibility

- Use semantic HTML elements
- Include ARIA labels and descriptions
- Ensure keyboard navigation works
- Maintain color contrast ratios
- Test with screen readers

## 🧪 Testing Guidelines

### Frontend Testing

```typescript
// Component tests with React Testing Library
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AgentCard } from "./AgentCard";

const createTestQueryClient = () =>
  new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

const renderWithProviders = (component: React.ReactElement) => {
  const queryClient = createTestQueryClient();
  return render(
    <QueryClientProvider client={queryClient}>{component}</QueryClientProvider>
  );
};

test("renders agent decision correctly", () => {
  const mockDecision = {
    agentName: "Warren Buffett",
    recommendation: "BUY" as const,
    confidence: 0.85,
    reasoning: "Strong fundamentals and undervalued stock",
  };

  renderWithProviders(<AgentCard decision={mockDecision} />);

  expect(screen.getByText("Warren Buffett")).toBeInTheDocument();
  expect(screen.getByText("BUY")).toBeInTheDocument();
  expect(screen.getByText("85%")).toBeInTheDocument();
});
```

### Backend Testing

```python
import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_get_agent_decisions():
    response = client.post("/api/v1/decisions", json={
        "tickers": ["AAPL", "MSFT"],
        "show_reasoning": True
    })

    assert response.status_code == 200
    data = response.json()
    assert len(data) > 0
    assert all("agentName" in decision for decision in data)
    assert all("recommendation" in decision for decision in data)

@pytest.fixture
def mock_agent_service(monkeypatch):
    def mock_process_decisions(*args, **kwargs):
        return [
            {
                "agentName": "Warren Buffett",
                "recommendation": "BUY",
                "confidence": 0.85,
                "reasoning": "Test reasoning"
            }
        ]

    monkeypatch.setattr("app.services.decision_service.process_decisions", mock_process_decisions)
```

## 🔄 Development Workflow

### Branch Naming

- `feature/agent-dashboard` - New features
- `bugfix/portfolio-calculation` - Bug fixes
- `refactor/api-endpoints` - Code refactoring
- `docs/api-documentation` - Documentation updates

### Commit Messages

Follow conventional commits format:

```
feat(dashboard): add real-time agent decision updates
fix(api): resolve portfolio calculation error
docs(readme): update setup instructions
refactor(components): extract reusable chart component
test(agents): add unit tests for decision logic
```

### Pull Request Process

1. **Create descriptive PR title** and detailed description
2. **Link related issues** using GitHub keywords
3. **Add screenshots** for UI changes
4. **Ensure all tests pass** and coverage is maintained
5. **Request review** from maintainers
6. **Address feedback** promptly

### Code Review Guidelines

- **Focus on logic and architecture** over style (automated tools handle style)
- **Check for security implications** especially in API endpoints
- **Verify error handling** and edge cases
- **Ensure accessibility** standards are met
- **Test the changes** locally when possible

## 🚀 Deployment

### Environment Setup

```bash
# Development
cp .env.example .env.development

# Staging
cp .env.example .env.staging

# Production
cp .env.example .env.production
```

### Docker Deployment

```bash
# Build and run with Docker Compose
docker-compose up --build

# Run specific services
docker-compose up frontend backend
```

## 📚 Additional Resources

- [React Best Practices](https://react.dev/learn)
- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Tailwind CSS](https://tailwindcss.com/docs)
- [TypeScript Handbook](https://www.typescriptlang.org/docs/)
- [Testing Library](https://testing-library.com/docs/)

## 🎯 Feature Guidelines

### Adding New Agents

1. Create agent class in `src/agents/`
2. Add agent to agent registry
3. Create corresponding UI components
4. Add tests for agent logic
5. Update documentation

### Adding New Charts

1. Create chart component in `app/frontend/src/components/charts/`
2. Use Recharts for consistency
3. Ensure responsive design
4. Add accessibility features
5. Include loading and error states

### API Endpoints

1. Define Pydantic schemas for request/response
2. Add proper error handling
3. Include API documentation
4. Add rate limiting if needed
5. Write comprehensive tests

## ⚠️ Important Notes

- **Educational Purpose**: Always include disclaimers about simulation vs. real trading
- **API Keys**: Never commit API keys or sensitive data
- **Performance**: Monitor bundle size and API response times
- **Security**: Validate all inputs and sanitize outputs
- **Accessibility**: Test with keyboard navigation and screen readers

## 🤝 Getting Help

- **GitHub Issues**: For bugs and feature requests
- **Discussions**: For questions and general discussion
- **Discord/Slack**: For real-time community support

Thank you for contributing to the AI Hedge Fund Web App! Your contributions help make this educational tool better for everyone. 🚀
