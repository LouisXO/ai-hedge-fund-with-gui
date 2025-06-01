import { useState } from 'react';
import { Dashboard } from './components/dashboard/Dashboard';
import { Portfolio } from './components/portfolio/Portfolio';
import { Layout } from './components/Layout';
import { Button } from './components/ui/button';
import { BarChart3, PieChart, TrendingUp, Settings } from 'lucide-react';

type ViewType = 'dashboard' | 'portfolio' | 'flow' | 'settings';

export default function App() {
  const [currentView, setCurrentView] = useState<ViewType>('dashboard');
  const [showLeftSidebar, setShowLeftSidebar] = useState(true);

  const navigationItems = [
    { id: 'dashboard', label: 'Dashboard', icon: BarChart3 },
    { id: 'portfolio', label: 'Portfolio', icon: PieChart },
    { id: 'flow', label: 'Agent Flow', icon: TrendingUp },
    { id: 'settings', label: 'Settings', icon: Settings },
  ] as const;

  const leftSidebar = showLeftSidebar ? (
    <div className="p-4 bg-gray-900 text-white h-full min-w-[240px]">
      <div className="mb-6">
        <h2 className="text-xl font-bold text-blue-400">AI Hedge Fund</h2>
        <p className="text-sm text-gray-400">Trading Intelligence</p>
      </div>
      
      <nav className="space-y-2">
        {navigationItems.map((item) => {
          const Icon = item.icon;
          return (
            <Button
              key={item.id}
              variant={currentView === item.id ? "default" : "ghost"}
              className={`w-full justify-start gap-3 ${
                currentView === item.id 
                  ? 'bg-blue-600 text-white hover:bg-blue-700' 
                  : 'text-gray-300 hover:text-white hover:bg-gray-800'
              }`}
              onClick={() => setCurrentView(item.id as ViewType)}
            >
              <Icon size={18} />
              {item.label}
            </Button>
          );
        })}
      </nav>
      
      <div className="absolute bottom-4 left-4 right-4">
        <Button
          variant="ghost"
          size="sm"
          className="w-full text-gray-400 hover:text-white"
          onClick={() => setShowLeftSidebar(false)}
        >
          Collapse
        </Button>
      </div>
    </div>
  ) : (
    <div className="p-2 bg-gray-900">
      <Button
        variant="ghost"
        size="sm"
        className="text-gray-400 hover:text-white"
        onClick={() => setShowLeftSidebar(true)}
      >
        →
      </Button>
    </div>
  );

  const renderCurrentView = () => {
    switch (currentView) {
      case 'dashboard':
        return <Dashboard />;
      case 'portfolio':
        return <Portfolio />;
      case 'flow':
        // Keep the existing Flow component for now
        const { Flow } = require('./components/Flow');
        return <Flow />;
      case 'settings':
        return (
          <div className="p-6 text-center">
            <h2 className="text-2xl font-bold mb-4">Settings</h2>
            <p className="text-gray-600">Settings panel coming soon...</p>
          </div>
        );
      default:
        return <Dashboard />;
    }
  };

  return (
    <Layout leftSidebar={leftSidebar}>
      {renderCurrentView()}
    </Layout>
  );
}
