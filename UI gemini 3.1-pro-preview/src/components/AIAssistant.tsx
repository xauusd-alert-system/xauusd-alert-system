import React, { useState, useRef, useEffect } from 'react';
import { Send, Bot, User, Sparkles, Zap, Search, BrainCircuit, ExternalLink } from 'lucide-react';
import { AIChatMessage } from '../types';
import { cn } from '../lib/utils';
import ReactMarkdown from 'react-markdown';

export const AIAssistant: React.FC = () => {
  const [messages, setMessages] = useState<AIChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [complexity, setComplexity] = useState<'fast' | 'general' | 'complex'>('general');
  const [enableSearch, setEnableSearch] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const handleSend = async () => {
    if (!input.trim() || isLoading) return;

    const userMessage: AIChatMessage = {
      id: Date.now().toString(),
      role: 'user',
      content: input,
      timestamp: Date.now(),
    };

    setMessages((prev) => [...prev, userMessage]);
    setInput('');
    setIsLoading(true);

    try {
      const response = await fetch('/api/ai/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: userMessage.content, complexity, enableSearch }),
      });

      if (!response.ok) {
        throw new Error('Failed to fetch response');
      }

      const data = await response.json();

      const assistantMessage: AIChatMessage = {
        id: (Date.now() + 1).toString(),
        role: 'assistant',
        content: data.reply,
        model: data.model,
        groundingChunks: data.groundingChunks,
        timestamp: Date.now(),
      };

      setMessages((prev) => [...prev, assistantMessage]);
    } catch (error) {
      console.error(error);
      const errorMessage: AIChatMessage = {
        id: (Date.now() + 1).toString(),
        role: 'assistant',
        content: 'Sorry, I encountered an error while processing your request.',
        timestamp: Date.now(),
      };
      setMessages((prev) => [...prev, errorMessage]);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="flex flex-col bg-slate-900 border border-slate-700/50 rounded-xl overflow-hidden h-[600px] shadow-2xl relative mt-8">
      {/* Header */}
      <div className="flex items-center justify-between p-4 bg-slate-800/80 border-b border-slate-700/50 shrink-0">
        <div className="flex items-center gap-3">
          <div className="p-2 bg-indigo-500/20 rounded-lg">
            <Sparkles className="w-5 h-5 text-indigo-400" />
          </div>
          <div>
            <h3 className="font-semibold text-slate-100">Trading Intelligence</h3>
            <p className="text-xs text-slate-400">Powered by Gemini</p>
          </div>
        </div>
        <div className="flex items-center gap-2 bg-slate-900/50 p-1.5 rounded-lg border border-slate-700/30">
          <button
            onClick={() => setComplexity('fast')}
            className={cn(
              "px-3 py-1.5 rounded-md text-xs font-medium transition-all flex items-center gap-1",
              complexity === 'fast' ? "bg-amber-500/20 text-amber-400 border border-amber-500/30" : "text-slate-400 hover:text-slate-300"
            )}
            title="Fast Response (Flash Lite)"
          >
            <Zap className="w-3 h-3" />
            Fast
          </button>
          <button
            onClick={() => setComplexity('general')}
            className={cn(
              "px-3 py-1.5 rounded-md text-xs font-medium transition-all flex items-center gap-1",
              complexity === 'general' ? "bg-emerald-500/20 text-emerald-400 border border-emerald-500/30" : "text-slate-400 hover:text-slate-300"
            )}
            title="General Purpose (Flash)"
          >
            <Sparkles className="w-3 h-3" />
            General
          </button>
          <button
            onClick={() => setComplexity('complex')}
            className={cn(
              "px-3 py-1.5 rounded-md text-xs font-medium transition-all flex items-center gap-1",
              complexity === 'complex' ? "bg-indigo-500/20 text-indigo-400 border border-indigo-500/30" : "text-slate-400 hover:text-slate-300"
            )}
            title="Deep Reasoning (Pro Preview)"
          >
            <BrainCircuit className="w-3 h-3" />
            Deep Thinking
          </button>
        </div>
      </div>

      {/* Settings Bar */}
      <div className="bg-slate-800/40 px-4 py-2 border-b border-slate-700/50 flex items-center shrink-0">
        <label className="flex items-center gap-2 cursor-pointer group">
          <input 
            type="checkbox" 
            className="hidden" 
            checked={enableSearch} 
            onChange={(e) => setEnableSearch(e.target.checked)}
            disabled={complexity === 'fast'}
          />
          <div className={cn(
            "w-4 h-4 rounded border flex items-center justify-center transition-colors",
            enableSearch ? "bg-blue-500 border-blue-500" : "border-slate-600 group-hover:border-slate-500",
            complexity === 'fast' && "opacity-50 cursor-not-allowed"
          )}>
            {enableSearch && <Search className="w-3 h-3 text-white" />}
          </div>
          <span className={cn(
            "text-xs font-medium",
            enableSearch ? "text-blue-400" : "text-slate-400",
            complexity === 'fast' && "opacity-50"
          )}>
            Google Search Grounding {complexity === 'fast' && '(Unavailable in Fast mode)'}
          </span>
        </label>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {messages.length === 0 && (
          <div className="h-full flex flex-col items-center justify-center text-slate-500 space-y-4">
            <div className="w-16 h-16 rounded-full bg-slate-800 flex items-center justify-center">
              <Bot className="w-8 h-8 text-slate-400" />
            </div>
            <p className="text-sm">Ask me to analyze the market, explain strategies, or retrieve news.</p>
          </div>
        )}
        
        {messages.map((msg) => (
          <div 
            key={msg.id} 
            className={cn(
              "flex gap-3 max-w-[85%]",
              msg.role === 'user' ? "ml-auto flex-row-reverse" : "mr-auto"
            )}
          >
            <div className={cn(
              "w-8 h-8 rounded-full flex items-center justify-center shrink-0",
              msg.role === 'user' ? "bg-blue-500/20 text-blue-400" : "bg-indigo-500/20 text-indigo-400"
            )}>
              {msg.role === 'user' ? <User className="w-4 h-4" /> : <Bot className="w-4 h-4" />}
            </div>
            <div className={cn(
              "rounded-2xl px-4 py-3 text-sm",
              msg.role === 'user' 
                ? "bg-blue-600/90 text-white rounded-tr-sm" 
                : "bg-slate-800 text-slate-200 rounded-tl-sm border border-slate-700/50"
            )}>
              <div className="markdown-body prose prose-invert max-w-none prose-p:leading-relaxed prose-pre:bg-slate-900 prose-pre:border prose-pre:border-slate-700">
                <ReactMarkdown>{msg.content}</ReactMarkdown>
              </div>
              
              {/* Metadata */}
              {msg.role === 'assistant' && (
                <div className="mt-3 pt-3 border-t border-slate-700/50 flex flex-wrap items-center gap-2 text-xs text-slate-400">
                  <span className="flex items-center gap-1 bg-slate-900/50 px-2 py-1 rounded">
                    <Sparkles className="w-3 h-3" />
                    {msg.model}
                  </span>
                  {msg.groundingChunks && msg.groundingChunks.length > 0 && (
                    <div className="w-full mt-2 space-y-1">
                      <p className="font-medium flex items-center gap-1 text-slate-300">
                        <Search className="w-3 h-3" /> Sources:
                      </p>
                      <ul className="flex flex-col gap-1">
                        {msg.groundingChunks.map((chunk, idx) => {
                          if (chunk.web?.uri) {
                            return (
                              <li key={idx}>
                                <a 
                                  href={chunk.web.uri} 
                                  target="_blank" 
                                  rel="noopener noreferrer"
                                  className="text-blue-400 hover:text-blue-300 flex items-center gap-1 truncate max-w-full"
                                >
                                  <ExternalLink className="w-3 h-3 shrink-0" />
                                  <span className="truncate">{chunk.web.title || chunk.web.uri}</span>
                                </a>
                              </li>
                            );
                          }
                          return null;
                        })}
                      </ul>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        ))}
        {isLoading && (
          <div className="flex gap-3 max-w-[85%] mr-auto">
            <div className="w-8 h-8 rounded-full bg-indigo-500/20 text-indigo-400 flex items-center justify-center shrink-0">
              <Bot className="w-4 h-4" />
            </div>
            <div className="rounded-2xl rounded-tl-sm px-4 py-3 bg-slate-800 border border-slate-700/50 flex items-center gap-2">
              <div className="w-2 h-2 bg-indigo-400 rounded-full animate-bounce [animation-delay:-0.3s]"></div>
              <div className="w-2 h-2 bg-indigo-400 rounded-full animate-bounce [animation-delay:-0.15s]"></div>
              <div className="w-2 h-2 bg-indigo-400 rounded-full animate-bounce"></div>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="p-4 bg-slate-800/80 border-t border-slate-700/50 shrink-0">
        <div className="flex gap-2 relative">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                handleSend();
              }
            }}
            placeholder="Ask about market conditions, strategy logic, or news..."
            className="flex-1 bg-slate-900 border border-slate-700 rounded-xl px-4 py-3 text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 resize-none max-h-32 min-h-[44px]"
            rows={input.split('\n').length > 1 ? Math.min(input.split('\n').length, 4) : 1}
          />
          <button
            onClick={handleSend}
            disabled={!input.trim() || isLoading}
            className="bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl px-4 flex items-center justify-center transition-colors disabled:opacity-50 disabled:cursor-not-allowed shrink-0"
          >
            <Send className="w-5 h-5" />
          </button>
        </div>
      </div>
    </div>
  );
};
