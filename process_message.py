from collections.abc import Callable
from collections.abc import Iterator
from functools import partial
from typing import cast

from sqlalchemy.orm import Session

from project.chat.chat_utils import create_chat_chain
from project.chat.models import CitationInfo
from project.chat.models import CustomToolResponse
from project.chat.models import projectAnswerPiece
from project.chat.models import ImageGenerationDisplay
from project.chat.models import LLMRelevanceFilterResponse
from project.chat.models import QADocsResponse
from project.chat.models import StreamingError
from project.configs.chat_configs import BING_API_KEY
from project.configs.chat_configs import CHAT_TARGET_CHUNK_PERCENTAGE
from project.configs.chat_configs import DISABLE_LLM_CHOOSE_SEARCH
from project.configs.chat_configs import MAX_CHUNKS_FED_TO_CHAT
from project.configs.constants import MessageType
from project.configs.model_configs import GEN_AI_TEMPERATURE
from project.db.chat import attach_files_to_chat_message, get_or_create_new_chat_session
from project.db.chat import create_db_search_doc
from project.db.chat import create_new_chat_message
from project.db.chat import get_chat_message
from project.db.chat import get_db_search_doc_by_id
from project.db.chat import get_doc_query_identifiers_from_model
from project.db.chat import get_or_create_root_message
from project.db.chat import translate_db_message_to_chat_message_detail
from project.db.chat import translate_db_search_doc_to_server_search_doc
from project.db.embedding_model import get_current_db_embedding_model
from project.db.engine import get_session_context_manager
from project.db.llm import fetch_existing_llm_providers
from project.db.models import SearchDoc as DbSearchDoc
from project.db.models import ToolCall
from project.db.models import User
from project.db.persona import get_persona_by_id
from project.document_index.factory import get_default_document_index
from project.file_store.models import ChatFileType
from project.file_store.models import FileDescriptor
from project.file_store.utils import load_all_chat_files
from project.file_store.utils import save_files_from_urls
from project.llm.answering.answer import Answer
from project.llm.answering.models import AnswerStyleConfig
from project.llm.answering.models import CitationConfig
from project.llm.answering.models import DocumentPruningConfig
from project.llm.answering.models import PreviousMessage
from project.llm.answering.models import PromptConfig
from project.llm.exceptions import GenAIDisabledException
from project.llm.factory import get_llms_for_persona
from project.llm.factory import get_main_llm_from_tuple
from project.llm.interfaces import LLMConfig
from project.natural_language_processing.utils import get_tokenizer
from project.search.enums import LLMEvaluationType
from project.search.enums import OptionalSearchSetting
from project.search.enums import QueryFlow
from project.search.enums import SearchType
from project.search.models import InferenceSection
from project.search.retrieval.search_runner import inference_sections_from_ids
from project.search.utils import chunks_or_sections_to_search_docs
from project.search.utils import dedupe_documents
from project.search.utils import drop_llm_indices
from project.search.utils import relevant_documents_to_indices
from project.server.query_and_chat.models import ChatMessageDetail
from project.server.query_and_chat.models import CreateChatMessageRequest
from project.server.utils import get_json_line
from project.tools.built_in_tools import get_built_in_tool_by_id
from project.tools.custom.custom_tool import build_custom_tools_from_openapi_schema
from project.tools.custom.custom_tool import CUSTOM_TOOL_RESPONSE_ID
from project.tools.custom.custom_tool import CustomToolCallSummary
from project.tools.force import ForceUseTool
from project.tools.images.image_generation_tool import IMAGE_GENERATION_RESPONSE_ID
from project.tools.images.image_generation_tool import ImageGenerationResponse
from project.tools.images.image_generation_tool import ImageGenerationTool
from project.tools.internet_search.internet_search_tool import (
    INTERNET_SEARCH_RESPONSE_ID,
)
from project.tools.internet_search.internet_search_tool import (
    internet_search_response_to_search_docs,
)
from project.tools.internet_search.internet_search_tool import InternetSearchResponse
from project.tools.internet_search.internet_search_tool import InternetSearchTool
from project.tools.search.search_tool import SEARCH_RESPONSE_SUMMARY_ID
from project.tools.search.search_tool import SearchResponseSummary
from project.tools.search.search_tool import SearchTool
from project.tools.search.search_tool import SECTION_RELEVANCE_LIST_ID
from project.tools.tool import Tool
from project.tools.tool import ToolResponse
from project.tools.tool_runner import ToolCallFinalResult
from project.tools.utils import compute_all_tool_tokens
from project.tools.utils import explicit_tool_calling_supported
from project.utils.logger import setup_logger
from project.utils.timing import log_generator_function_time

logger = setup_logger()


ChatPacket = (
    StreamingError
    | QADocsResponse
    | LLMRelevanceFilterResponse
    | ChatMessageDetail
    | projectAnswerPiece
    | CitationInfo
    | ImageGenerationDisplay
    | CustomToolResponse
)
ChatPacketStream = Iterator[ChatPacket]


def stream_chat_message_objects(
    new_msg_req: CreateChatMessageRequest,
    user: User | None,
    db_session: Session,
    # Needed to translate persona num_chunks to tokens to the LLM
    default_num_chunks: float = MAX_CHUNKS_FED_TO_CHAT,
    # For flow with search, don't include as many chunks as possible since we need to leave space
    # for the chat history, for smaller models, we likely won't get MAX_CHUNKS_FED_TO_CHAT chunks
    max_document_percentage: float = CHAT_TARGET_CHUNK_PERCENTAGE,
    # if specified, uses the last user message and does not create a new user message based
    # on the `new_msg_req.message`. Currently, requires a state where the last message is a
    # user message (e.g. this can only be used for the chat-seeding flow).
    use_existing_user_message: bool = False,
    litellm_additional_headers: dict[str, str] | None = None,
) -> ChatPacketStream:
    """Streams in order:
    1. [conditional] Retrieved documents if a search needs to be run
    2. [conditional] LLM selected chunk indices if LLM chunk filtering is turned on
    3. [always] A set of streamed LLM tokens or an error anywhere along the line if something fails
    4. [always] Details on the final AI response message that is created
    """
    try:
        user_id = user.id if user is not None else None

        # Create a new chat session if end user start a new chat
        chat_session = get_or_create_new_chat_session(
            db_session,
            user_id,
            new_msg_req)

        message_text = new_msg_req.message
        chat_session_id = chat_session.id
        parent_id = new_msg_req.parent_message_id
        reference_doc_ids = new_msg_req.search_doc_ids
        retrieval_options = new_msg_req.retrieval_options
        alternate_assistant_id = new_msg_req.alternate_assistant_id

        # use alternate persona if alternative assistant id is passed in
        if alternate_assistant_id is not None:
            persona = get_persona_by_id(
                alternate_assistant_id,
                user=user,
                db_session=db_session,
                is_for_edit=False,
            )
        else:
            persona = chat_session.persona

        prompt_id = new_msg_req.prompt_id
        if prompt_id is None and persona.prompts:
            prompt_id = sorted(persona.prompts, key=lambda x: x.id)[-1].id

        if reference_doc_ids is None and retrieval_options is None:
            raise RuntimeError(
                "Must specify a set of documents for chat or specify search options"
            )

        try:
            llm, fast_llm = get_llms_for_persona(
                persona=persona,
                llm_override=new_msg_req.llm_override or chat_session.llm_override,
                additional_headers=litellm_additional_headers,
            )
        except GenAIDisabledException:
            raise RuntimeError("LLM is disabled. Can't use chat flow without LLM.")

        llm_provider = llm.config.model_provider
        llm_model_name = llm.config.model_name

        llm_tokenizer = get_tokenizer(
            model_name=llm_model_name,
            provider_type=llm_provider,
        )
        llm_tokenizer_encode_func = cast(
            Callable[[str], list[int]], llm_tokenizer.encode
        )

        embedding_model = get_current_db_embedding_model(db_session)
        document_index = get_default_document_index(
            primary_index_name=embedding_model.index_name, secondary_index_name=None
        )

        # Every chat Session begins with an empty root message
        root_message = get_or_create_root_message(
            chat_session_id=chat_session_id, db_session=db_session
        )

        if parent_id is not None:
            parent_message = get_chat_message(
                chat_message_id=parent_id,
                user_id=user_id,
                db_session=db_session,
            )
        else:
            parent_message = root_message

        user_message = None
        if not use_existing_user_message:
            # Create new message at the right place in the tree and update the parent's child pointer
            # Don't commit yet until we verify the chat message chain
            user_message = create_new_chat_message(
                chat_session_id=chat_session_id,
                parent_message=parent_message,
                prompt_id=prompt_id,
                message=message_text,
                token_count=len(llm_tokenizer_encode_func(message_text)),
                message_type=MessageType.USER,
                files=None,  # Need to attach later for optimization to only load files once in parallel
                db_session=db_session,
                commit=False,
            )
            # re-create linear history of messages
            final_msg, history_msgs = create_chat_chain(
                chat_session_id=chat_session_id, db_session=db_session
            )
            if final_msg.id != user_message.id:
                db_session.rollback()
                raise RuntimeError(
                    "The new message was not on the mainline. "
                    "Be sure to update the chat pointers before calling this."
                )

            # NOTE: do not commit user message - it will be committed when the
            # assistant message is successfully generated
        else:
            # re-create linear history of messages
            final_msg, history_msgs = create_chat_chain(
                chat_session_id=chat_session_id, db_session=db_session
            )
            if final_msg.message_type != MessageType.USER:
                raise RuntimeError(
                    "The last message was not a user message. Cannot call "
                    "`stream_chat_message_objects` with `is_regenerate=True` "
                    "when the last message is not a user message."
                )

        # Disable Query Rephrasing for the first message
        # This leads to a better first response since the LLM rephrasing the question
        # leads to worst search quality
        if not history_msgs:
            new_msg_req.query_override = (
                new_msg_req.query_override or new_msg_req.message
            )

        # load all files needed for this chat chain in memory
        files = load_all_chat_files(
            history_msgs, new_msg_req.file_descriptors, db_session
        )
        latest_query_files = [
            file
            for file in files
            if file.file_id in [f["id"] for f in new_msg_req.file_descriptors]
        ]

        if user_message:
            attach_files_to_chat_message(
                chat_message=user_message,
                files=[
                    new_file.to_file_descriptor() for new_file in latest_query_files
                ],
                db_session=db_session,
                commit=False,
            )

        selected_db_search_docs = None
        selected_sections: list[InferenceSection] | None = None
        if reference_doc_ids:
            identifier_tuples = get_doc_query_identifiers_from_model(
                search_doc_ids=reference_doc_ids,
                chat_session=chat_session,
                user_id=user_id,
                db_session=db_session,
            )

            # Generates full documents currently
            # May extend to use sections instead in the future
            selected_sections = inference_sections_from_ids(
                doc_identifiers=identifier_tuples,
                document_index=document_index,
            )
            document_pruning_config = DocumentPruningConfig(
                is_manually_selected_docs=True
            )

            # In case the search doc is deleted, just don't include it
            # though this should never happen
            db_search_docs_or_none = [
                get_db_search_doc_by_id(doc_id=doc_id, db_session=db_session)
                for doc_id in reference_doc_ids
            ]

            selected_db_search_docs = [
                db_sd for db_sd in db_search_docs_or_none if db_sd
            ]

        else:
            document_pruning_config = DocumentPruningConfig(
                max_chunks=int(
                    persona.num_chunks
                    if persona.num_chunks is not None
                    else default_num_chunks
                ),
                max_window_percentage=max_document_percentage,
                use_sections=new_msg_req.chunks_above > 0
                or new_msg_req.chunks_below > 0,
            )

        # Cannot determine these without the LLM step or breaking out early
        partial_response = partial(
            create_new_chat_message,
            chat_session_id=chat_session_id,
            parent_message=final_msg,
            prompt_id=prompt_id,
            # message=,
            # rephrased_query=,
            # token_count=,
            message_type=MessageType.ASSISTANT,
            alternate_assistant_id=new_msg_req.alternate_assistant_id,
            # error=,
            # reference_docs=,
            db_session=db_session,
            commit=False,
        )

        if not final_msg.prompt:
            raise RuntimeError("No Prompt found")

        prompt_config = (
            PromptConfig.from_model(
                final_msg.prompt,
                prompt_override=(
                    new_msg_req.prompt_override or chat_session.prompt_override
                ),
            )
            if not persona
            else PromptConfig.from_model(persona.prompts[0])
        )

        # find out what tools to use
        search_tool: SearchTool | None = None
        tool_dict: dict[int, list[Tool]] = {}  # tool_id to tool
        for db_tool_model in persona.tools:
            # handle in-code tools specially
            if db_tool_model.in_code_tool_id:
                tool_cls = get_built_in_tool_by_id(db_tool_model.id, db_session)
                if tool_cls.__name__ == SearchTool.__name__ and not latest_query_files:
                    search_tool = SearchTool(
                        db_session=db_session,
                        user=user,
                        persona=persona,
                        retrieval_options=retrieval_options,
                        prompt_config=prompt_config,
                        llm=llm,
                        fast_llm=fast_llm,
                        pruning_config=document_pruning_config,
                        selected_sections=selected_sections,
                        chunks_above=new_msg_req.chunks_above,
                        chunks_below=new_msg_req.chunks_below,
                        full_doc=new_msg_req.full_doc,
                        evaluation_type=LLMEvaluationType.BASIC
                        if persona.llm_relevance_filter
                        else LLMEvaluationType.SKIP,
                    )
                    tool_dict[db_tool_model.id] = [search_tool]
                elif tool_cls.__name__ == ImageGenerationTool.__name__:
                    img_generation_llm_config: LLMConfig | None = None
                    if (
                        llm
                        and llm.config.api_key
                        and llm.config.model_provider == "openai"
                    ):
                        img_generation_llm_config = llm.config
                    else:
                        llm_providers = fetch_existing_llm_providers(db_session)
                        openai_provider = next(
                            iter(
                                [
                                    llm_provider
                                    for llm_provider in llm_providers
                                    if llm_provider.provider == "openai"
                                ]
                            ),
                            None,
                        )
                        if not openai_provider or not openai_provider.api_key:
                            raise ValueError(
                                "Image generation tool requires an OpenAI API key"
                            )
                        img_generation_llm_config = LLMConfig(
                            model_provider=openai_provider.provider,
                            model_name=openai_provider.default_model_name,
                            temperature=GEN_AI_TEMPERATURE,
                            api_key=openai_provider.api_key,
                            api_base=openai_provider.api_base,
                            api_version=openai_provider.api_version,
                        )
                    tool_dict[db_tool_model.id] = [
                        ImageGenerationTool(
                            api_key=cast(str, img_generation_llm_config.api_key),
                            api_base=img_generation_llm_config.api_base,
                            api_version=img_generation_llm_config.api_version,
                            additional_headers=litellm_additional_headers,
                        )
                    ]
                elif tool_cls.__name__ == InternetSearchTool.__name__:
                    bing_api_key = BING_API_KEY
                    if not bing_api_key:
                        raise ValueError(
                            "Internet search tool requires a Bing API key, please contact your project admin to get it added!"
                        )
                    tool_dict[db_tool_model.id] = [
                        InternetSearchTool(api_key=bing_api_key)
                    ]

                continue

            # handle all custom tools
            if db_tool_model.openapi_schema:
                tool_dict[db_tool_model.id] = cast(
                    list[Tool],
                    build_custom_tools_from_openapi_schema(
                        db_tool_model.openapi_schema
                    ),
                )

        tools: list[Tool] = []
        for tool_list in tool_dict.values():
            tools.extend(tool_list)

        # factor in tool definition size when pruning
        document_pruning_config.tool_num_tokens = compute_all_tool_tokens(
            tools, llm_tokenizer
        )
        document_pruning_config.using_tool_message = explicit_tool_calling_supported(
            llm_provider, llm_model_name
        )

        # LLM prompt building, response capturing, etc.
        answer = Answer(
            question=final_msg.message,
            latest_query_files=latest_query_files,
            answer_style_config=AnswerStyleConfig(
                citation_config=CitationConfig(
                    all_docs_useful=selected_db_search_docs is not None
                ),
                document_pruning_config=document_pruning_config,
            ),
            prompt_config=prompt_config,
            llm=(
                llm
                or get_main_llm_from_tuple(
                    get_llms_for_persona(
                        persona=persona,
                        llm_override=(
                            new_msg_req.llm_override or chat_session.llm_override
                        ),
                        additional_headers=litellm_additional_headers,
                    )
                )
            ),
            message_history=[
                PreviousMessage.from_chat_message(msg, files) for msg in history_msgs
            ],
            tools=tools,
            force_use_tool=_get_force_search_settings(new_msg_req, tools),
        )

        reference_db_search_docs = None
        qa_docs_response = None
        ai_message_files = None  # any files to associate with the AI message e.g. dall-e generated images
        dropped_indices = None
        tool_result = None
        for packet in answer.processed_streamed_output:
            if isinstance(packet, ToolResponse):
                if packet.id == SEARCH_RESPONSE_SUMMARY_ID:
                    (
                        qa_docs_response,
                        reference_db_search_docs,
                        dropped_indices,
                    ) = _handle_search_tool_response_summary(
                        packet=packet,
                        db_session=db_session,
                        selected_search_docs=selected_db_search_docs,
                        # Deduping happens at the last step to avoid harming quality by dropping content early on
                        dedupe_docs=retrieval_options.dedupe_docs
                        if retrieval_options
                        else False,
                    )
                    yield qa_docs_response
                elif packet.id == SECTION_RELEVANCE_LIST_ID:
                    relevance_sections = packet.response

                    if reference_db_search_docs is not None:
                        llm_indices = relevant_documents_to_indices(
                            relevance_sections=relevance_sections,
                            search_docs=[
                                translate_db_search_doc_to_server_search_doc(doc)
                                for doc in reference_db_search_docs
                            ],
                        )

                        if dropped_indices:
                            llm_indices = drop_llm_indices(
                                llm_indices=llm_indices,
                                search_docs=reference_db_search_docs,
                                dropped_indices=dropped_indices,
                            )

                        yield LLMRelevanceFilterResponse(
                            relevant_chunk_indices=llm_indices
                        )

                elif packet.id == IMAGE_GENERATION_RESPONSE_ID:
                    img_generation_response = cast(
                        list[ImageGenerationResponse], packet.response
                    )

                    file_ids = save_files_from_urls(
                        [img.url for img in img_generation_response]
                    )
                    ai_message_files = [
                        FileDescriptor(id=str(file_id), type=ChatFileType.IMAGE)
                        for file_id in file_ids
                    ]
                    yield ImageGenerationDisplay(
                        file_ids=[str(file_id) for file_id in file_ids]
                    )
                elif packet.id == INTERNET_SEARCH_RESPONSE_ID:
                    (
                        qa_docs_response,
                        reference_db_search_docs,
                    ) = _handle_internet_search_tool_response_summary(
                        packet=packet,
                        db_session=db_session,
                    )
                    yield qa_docs_response
                elif packet.id == CUSTOM_TOOL_RESPONSE_ID:
                    custom_tool_response = cast(CustomToolCallSummary, packet.response)
                    yield CustomToolResponse(
                        response=custom_tool_response.tool_result,
                        tool_name=custom_tool_response.tool_name,
                    )

            else:
                if isinstance(packet, ToolCallFinalResult):
                    tool_result = packet
                yield cast(ChatPacket, packet)

    except Exception as e:
        error_msg = str(e)
        logger.exception(f"Failed to process chat message: {error_msg}")

        if "Illegal header value b'Bearer  '" in error_msg:
            error_msg = (
                f"Authentication error: Invalid or empty API key provided for '{llm.config.model_provider}'. "
                "Please check your API key configuration."
            )
        elif (
            "Invalid leading whitespace, reserved character(s), or return character(s) in header value"
            in error_msg
        ):
            error_msg = (
                f"Authentication error: Invalid API key format for '{llm.config.model_provider}'. "
                "Please ensure your API key does not contain leading/trailing whitespace or invalid characters."
            )
        elif llm.config.api_key and llm.config.api_key.lower() in error_msg.lower():
            error_msg = f"LLM failed to respond. Invalid API key error from '{llm.config.model_provider}'."
        else:
            error_msg = "An unexpected error occurred while processing your request. Please try again later."

        yield StreamingError(error=error_msg)
        db_session.rollback()
        return

    # Post-LLM answer processing
    try:
        db_citations = None
        if reference_db_search_docs:
            db_citations = translate_citations(
                citations_list=answer.citations,
                db_docs=reference_db_search_docs,
            )

        # Saving Gen AI answer and responding with message info
        tool_name_to_tool_id: dict[str, int] = {}
        for tool_id, tool_list in tool_dict.items():
            for tool in tool_list:
                tool_name_to_tool_id[tool.name] = tool_id

        gen_ai_response_message = partial_response(
            message=answer.llm_answer,
            rephrased_query=(
                qa_docs_response.rephrased_query if qa_docs_response else None
            ),
            reference_docs=reference_db_search_docs,
            files=ai_message_files,
            token_count=len(llm_tokenizer_encode_func(answer.llm_answer)),
            citations=db_citations,
            error=None,
            tool_calls=[
                ToolCall(
                    tool_id=tool_name_to_tool_id[tool_result.tool_name],
                    tool_name=tool_result.tool_name,
                    tool_arguments=tool_result.tool_args,
                    tool_result=tool_result.tool_result,
                )
            ]
            if tool_result
            else [],
        )
        db_session.commit()  # actually save user / assistant message

        msg_detail_response = translate_db_message_to_chat_message_detail(
            gen_ai_response_message
        )

        yield msg_detail_response
    except Exception as e:
        logger.exception(e)

        # Frontend will erase whatever answer and show this instead
        yield StreamingError(error="Failed to parse LLM output")


@log_generator_function_time()
def stream_chat_message(
    new_msg_req: CreateChatMessageRequest,
    user: User | None,
    use_existing_user_message: bool = False,
    litellm_additional_headers: dict[str, str] | None = None,
) -> Iterator[str]:
    with get_session_context_manager() as db_session:
        objects = stream_chat_message_objects(
            new_msg_req=new_msg_req,
            user=user,
            db_session=db_session,
            use_existing_user_message=use_existing_user_message,
            litellm_additional_headers=litellm_additional_headers,
        )
        for obj in objects:
            yield get_json_line(obj.dict())
