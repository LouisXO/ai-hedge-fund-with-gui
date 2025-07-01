import {
  BadgeDollarSign,
  BarChart3,
  Bot,
  Brain,
  Crown,
  Calculator,
  FileJson,
  FileText,
  Globe,
  LucideIcon,
  Shield,
  TrendingUp,
  Monitor,
  Network,
  Play
} from 'lucide-react';
import { Agent, getAgents } from './agents';

// Define component items by group
export interface ComponentItem {
  name: string;
  icon: LucideIcon;
}

export interface ComponentGroup {
  name: string;
  icon: LucideIcon;
  iconColor: string;
  items: ComponentItem[];
}

// Define all component groups and items
export const componentGroups: ComponentGroup[] = [
  {
    name: "agents",
    icon: Bot,
    iconColor: "text-red-400",
    items: agents.map(agent => ({
      name: agent.display_name,
      icon: Bot
    }))
  },
  {
    name: "inputs",
    icon: ArrowDownToLine,
    iconColor: "text-blue-400",
    items: [
      // { name: "Chat Input", icon: MessageSquare },
      { name: "Portfolio Manager", icon: Brain },
      // { name: "File Input", icon: FileText }
    ]
  },
  {
    name: "outputs",
    icon: ArrowUpFromLine,
    iconColor: "text-green-400",
    items: [
      { name: "JSON Output", icon: FileJson },
      { name: "Investment Report", icon: FileText },
    ]
  },
  {
    name: "swarms",
    icon: Users,
    iconColor: "text-yellow-400",
    items: [
      { name: "Data Wizards", icon: BarChart },
      { name: "Value Investors", icon: BadgeDollarSign },
      { name: "Growth Investors", icon: TrendingUp },
      { name: "Contrarian Investors", icon: Shield },
      { name: "Technical Analysis Team", icon: BarChart3 },
      { name: "Fundamental Analysis Team", icon: Brain },
      { name: "Macro Strategy Team", icon: Globe },
      { name: "All Agents", icon: Crown },
    ]
  }
]; 