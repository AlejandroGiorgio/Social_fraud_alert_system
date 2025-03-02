from typing import Dict, List, Optional, Any, Type
from datetime import datetime
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_core.prompts import (
    SystemMessagePromptTemplate,
    HumanMessagePromptTemplate,
    ChatPromptTemplate,
)
from langgraph.graph import StateGraph, END
import json
from src.nodes.utils import FraudTypeRegistry
from src.settings import Settings
from langchain_core.messages import BaseMessage

setting = Settings()


class FraudAnalysis(BaseModel):
    """Data class to store fraud analysis results."""

    is_fraud: bool
    fraud_type: str
    explanation: str
    similar_cases: List[str]
    timestamp: datetime
    new_type_name: Optional[str] = None


# Existing State and Input models
class CuratorState(BaseModel):
    """State model for the fraud detection workflow."""

    text: str
    similar_cases: List[str]
    pattern_analysis: Optional[Dict] = None
    fraud_type: Optional[Dict] = None
    final_summary: Optional[str] = None
    is_fraud: bool = False
    new_type_name: Optional[str] = None


class TextInput(BaseModel):
    text: str = Field(..., description="The text to analyze")


# New Pydantic models for validation
class PatternAnalysisOutput(BaseModel):
    is_fraud: bool
    patterns: List[str]
    reasoning: str


class FraudTypeOutput(BaseModel):
    fraud_type: str
    explanation: str
    new_type_name: Optional[str] = None


class FraudSummaryOutput(BaseModel):
    summary: str
    warning_signs: List[str]
    precautions: List[str]


class PostResponseInput(BaseModel):
    content: Dict[str, Any] = Field(
        ..., description="The content to validate and format"
    )
    model: Type[BaseModel] = Field(
        ..., description="The Pydantic model to validate against"
    )


def post_response(input_data: PostResponseInput) -> Dict:
    """Tool for agents to post their responses in the correct format."""
    return input_data.model.model_validate(input_data.content).model_dump()


# Agent base class with retry logic
class BaseAgent:
    output_model: Type[BaseModel] = None

    def __init__(self, llm: ChatOpenAI):
        if not self.output_model:
            raise ValueError("output_model must be defined in child class")

        # Configure LLM with structured output validation using the output_model
        self.llm = llm.with_structured_output(self.output_model)

    def _validate_and_parse_response(self, response: BaseMessage) -> Dict:
        """Assume response is already validated and parsed by the LLM."""
        try:
            # Directly return the response as it is already structured
            return response

        except Exception as e:
            print(f"Error in processing response: {str(e)}")
            print(f"Original response: {response}")
            raise e


class PatternAnalysisAgent(BaseAgent):
    output_model = PatternAnalysisOutput

    def __init__(self, llm: ChatOpenAI):
        super().__init__(llm)
        self.prompt = ChatPromptTemplate.from_messages(
            [
                SystemMessagePromptTemplate.from_template(
                    "You are an experienced fraud analyst with expertise in pentesting.\n"
                    "You will be presented with a case that has been sent to you by a concerned user.\n"
                    "Keep in mind that the user does not have technical knowledge and may be misinformed.\n"
                    "Your task is to determine if the case presented to you is really a potential fraud or not.\n"
                    "In order to do this, you will need to analyze the text for potential fraud patterns and provide a clear explanation of your reasoning.\n"
                    "If you believe the case is a fraud, set is_fraud to True, provide a list of potential fraud patterns, and a clear explanation of your reasoning.\n"
                    "If you believe the case is not a fraud, set is_fraud to False and provide a clear explanation of your reasoning.\n"
                    "You only classify the case as fraud if there are clear fraud patterns present that imply an illegal activity.\n"
                    "Customer-seller active disputes with no deception, mistreatment, or other unethical behavior are not considered fraud.\n"
                    "If the case describes a scenario that is not technically possible or is based on misinformation or misunderstanding (e.g., scientifically implausible hacking methods), set is_fraud to False.\n"
                    "Provide an explanation of why the scenario is invalid, and, if relevant, indicate that it may stem from public misconception.\n"
                    "If you don't have enough information to make a decision, set is_fraud to False and provide an explanation of why you are unsure.\n"
                    "if you are unsure of the technical plausible of the scenario, set is_fraud to False and provide an explanation of why you are unsure."
                ),
                HumanMessagePromptTemplate.from_template(
                    """Analyze this text for potential fraud patterns:
                Text: {text}"""
                ),
            ]
        )

    def analyze(self, text: str) -> PatternAnalysisOutput:
        response = self.llm.invoke(self.prompt.format_messages(text=text))
        return self._validate_and_parse_response(response)


class FraudTypeAgent(BaseAgent):
    output_model = FraudTypeOutput

    def __init__(self, llm: ChatOpenAI, type_registry: "FraudTypeRegistry"):
        super().__init__(llm)
        self.type_registry = type_registry
        self.prompt = ChatPromptTemplate.from_messages(
            [
                SystemMessagePromptTemplate.from_template(
                    "You are a fraud expert specialized in pattern recognition and taxonomy.\n"
                    "Your primary task is to classify fraud cases into broad, reusable categories that capture the fundamental nature of the fraud, avoiding over-segmentation.\n"
                    "Focus on identifying the core deceptive mechanism that defines the fraud, rather than its specific implementation details or context.\n"
                    "Prioritize grouping cases by their underlying pattern, emphasizing broad and inclusive categories over narrow and specific ones.\n"
                    "When encountering minor variations within a fraud category, consider whether these can be subsumed under an existing broader category.\n"
                    "Suggest a new type only if it represents a genuinely novel and fundamentally distinct deceptive mechanism from all existing types.\n"
                    "Before proposing a new type, exhaust all possibilities of fitting the case into existing categories, recognizing that minor semantic differences do not necessarily warrant a new type.\n"
                    "Provide the following outputs:\n"
                    "- fraud_type: Use an existing type whenever possible; use 'NEW' only if absolutely necessary.\n"
                    "- new_type_name: Only if fraud_type is 'NEW', describe the core deceptive pattern in upper case.\n"
                    "- explanation: Justify classification focusing on the core deceptive mechanism, emphasizing the rationale for not creating unnecessary new categories.\n"
                ),
                HumanMessagePromptTemplate.from_template(
                    """Classify this fraud pattern:
                Description: {description}
                
                Known fraud types: {known_types}"""
                ),
            ]
        )

    def classify(
        self, pattern_description: str, similar_cases: list
    ) -> FraudTypeOutput:
        response = self.llm.invoke(
            self.prompt.format_messages(
                description=pattern_description,
                known_types=similar_cases,
            )
        )
        return self._validate_and_parse_response(response)


class SummaryAgent(BaseAgent):
    output_model = FraudSummaryOutput

    def __init__(self, llm: ChatOpenAI):
        super().__init__(llm)
        self.prompt = ChatPromptTemplate.from_messages(
            [
                SystemMessagePromptTemplate.from_template(
                    """You are a fraud prevention expert.
                Generate a clear, abstract, and concise summary of this fraud analysis.
                Include specific warning signs and practical precautions to prevent similar frauds."""
                ),
                HumanMessagePromptTemplate.from_template(
                    """Generate a clear, concise summary of this fraud analysis:
                {analysis}"""
                ),
            ]
        )

    def summarize(self, analysis: Dict) -> FraudSummaryOutput:
        response = self.llm.invoke(self.prompt.format_messages(analysis=analysis))
        return self._validate_and_parse_response(response)


# Modified workflow creation function
def create_curator_graph(
    pattern_agent: PatternAnalysisAgent,
    type_agent: FraudTypeAgent,
    summary_agent: SummaryAgent,
) -> StateGraph:
    """Create the curator workflow graph with agents."""
    workflow = StateGraph(CuratorState)

    # Node functions that use the agents
    def analyze_patterns(state: Dict) -> Dict:
        state = CuratorState.model_validate(state)
        result = pattern_agent.analyze(state.text)
        try:
            result_model = PatternAnalysisOutput.model_validate(result)
        except Exception as e:
            raise ValueError(
                f"Error al validar el resultado de PatternAnalysisAgent: {e}"
            )
        state.pattern_analysis = result_model.model_dump()  # Serializar para el estado
        state.is_fraud = result_model.is_fraud
        return state.model_dump()

    def classify_type(state: Dict) -> Dict:
        state = CuratorState.model_validate(state)
        result = type_agent.classify(state.pattern_analysis, state.similar_cases)
        try:
            result_model = FraudTypeOutput.model_validate(result)
        except Exception as e:
            raise ValueError(f"Error al validar el resultado de FraudTypeAgent: {e}")
        state.fraud_type = result_model.model_dump()
        if state.fraud_type["fraud_type"] == "NEW":
            state.new_type_name = state.fraud_type["new_type_name"]
        print("State after classify: ", state)
        return state.model_dump()

    def generate_summary(state: Dict) -> Dict:
        state = CuratorState.model_validate(state)
        result = summary_agent.summarize(
            {
                "pattern_analysis": state.pattern_analysis,
                "fraud_type": state.fraud_type,
            }
        )
        # Validar y convertir el resultado al modelo esperado
        try:
            result_model = FraudSummaryOutput.model_validate(result)
        except Exception as e:
            raise ValueError(f"Error al validar el resultado de SummaryAgent: {e}")
        print(state)
        state.final_summary = result_model.summary
        return state.model_dump()

    # Add nodes
    workflow.add_node("analyze_patterns", analyze_patterns)
    workflow.add_node("classify_type", classify_type)
    workflow.add_node("generate_summary", generate_summary)

    # Define conditional routing
    def should_classify(state: Dict) -> str:
        state = CuratorState.model_validate(state)
        return "classify" if state.is_fraud else "end"

    # Add edges
    workflow.add_conditional_edges(
        "analyze_patterns", should_classify, {"classify": "classify_type", "end": END}
    )
    workflow.add_edge("classify_type", "generate_summary")
    workflow.add_edge("generate_summary", END)

    workflow.set_entry_point("analyze_patterns")

    return workflow.compile()


# Modified CuratorAgent class
class CuratorAgent:
    """CuratorAgent that uses OpenAI for inference with validated agents"""

    def __init__(self):

        settings = Settings()

        self.llm = ChatOpenAI(model=settings.OPENAI_LLM_MODEL, temperature=0.0)

        self.type_registry = FraudTypeRegistry()

        # Initialize agents
        self.pattern_agent = PatternAnalysisAgent(self.llm)
        self.type_agent = FraudTypeAgent(self.llm, self.type_registry)
        self.summary_agent = SummaryAgent(self.llm)

        # Create workflow with agents
        self.workflow = create_curator_graph(
            self.pattern_agent, self.type_agent, self.summary_agent
        )

    def analyze_case(
        self, text_input: TextInput, similar_cases: List[str]
    ) -> FraudAnalysis:
        """Analyze a potential fraud case."""
        initial_state = CuratorState(
            text=text_input.text, similar_cases=similar_cases
        ).model_dump()

        final_state = self.workflow.invoke(initial_state)
        final_state = CuratorState.model_validate(final_state)

        if final_state.is_fraud:
            return FraudAnalysis(
                is_fraud=True,
                fraud_type=final_state.fraud_type["fraud_type"],
                explanation=final_state.final_summary
                or final_state.pattern_analysis["reasoning"],
                similar_cases=similar_cases,
                timestamp=datetime.now(),
                new_type_name=final_state.new_type_name,
            )

        return FraudAnalysis(
            is_fraud=False,
            fraud_type="NO_FRAUD",
            explanation=final_state.pattern_analysis["reasoning"],
            similar_cases=similar_cases,
            timestamp=datetime.now(),
        )
