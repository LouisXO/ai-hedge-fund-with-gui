import {
  ArrowDownToLine,
  ArrowUpFromLine,
  BarChart3,
  Bot,
  Brain,
  FileText,
  Lightbulb,
  TrendingUp,
  Users,
  Zap
} from 'lucide-react';

// Define component items by group
export interface ComponentItem {
  name: string;
  icon: React.ComponentType;
  description?: string;
}

export interface ComponentGroup {
  name: string;
  items: ComponentItem[];
  icon: React.ComponentType;
}

// Define all component groups and items
export const componentGroups: ComponentGroup[] = [
  {
    name: "AI Agents",
    icon: Bot,
    items: [] // Will be populated dynamically from API
  },
  {
    name: "Data Sources",
    icon: ArrowDownToLine,
    items: [
      { name: "Stock Data", icon: TrendingUp },
      { name: "News Feed", icon: FileText },
      { name: "Market Data", icon: BarChart3 }
    ]
  },
  {
    name: "Outputs",
    icon: ArrowUpFromLine,
    items: [
      { name: "Portfolio Report", icon: FileText },
      { name: "Trading Signals", icon: Zap },
      { name: "Risk Analysis", icon: Brain }
    ]
  },
  {
    name: "Collaboration",
    icon: Users,
    items: [
      { name: "Data Wizards", icon: BarChart3 },
      { name: "Insight Agents", icon: Lightbulb }
    ]
  }
]; 